#!/usr/bin/env python3
"""Prepare Claude Code subagent files and expose workflow agents to SendMessage."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import secrets
import shlex
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple


AGENT_ID_RE = re.compile(r"^a(?:[A-Za-z0-9_-]{1,63}-)?[0-9a-f]{16}$")
WORKFLOW_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9_-]*$")
DEFAULT_CALLER_DISCOVERY_TIMEOUT_SECONDS = 5.0
MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS = 60.0
CALLER_DISCOVERY_POLL_INTERVAL_SECONDS = 0.025
CALLER_DISCOVERY_SETTLE_SECONDS = 0.25
CALLER_INVOCATION_MAX_AGE_SECONDS = 10.0
CALLER_INVOCATION_ENV = "SUPER_SUBAGENT_INVOCATION_ID"
CALLER_INVOCATION_RE = re.compile(r"^[0-9a-f]{32}$")
STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "named",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
FORK_BOILERPLATE = """<fork-boilerplate>
You are a worker fork. The transcript above is the parent's history: inherited reference, not your situation. You are NOT a continuation of that agent. Execute ONE directive, then stop.

Hard rules:
- Do NOT spawn subagents with the Agent tool. The "default to forking" guidance is for the parent; you ARE the fork, execute directly.
- One shot: report once and stop. No follow-up questions, no proposed next steps, no waiting for the user.

Guidelines (your directive may override any of these):
- Stay in scope. Other forks may be handling adjacent work; if you spot something outside your directive, note it in a sentence and move on.
- Open with one line restating your task, so the parent can spot scope drift at a glance.
- Be concise: as short as the answer allows, no shorter. Plain text, no preamble, no meta-commentary.
- If you committed changes, list the paths and commit hashes in your report.
</fork-boilerplate>"""


class UsageError(RuntimeError):
    pass


class PrepareInvocation(NamedTuple):
    source_id: str | None
    transcript: Path
    assistant_uuid: str
    response_id: str | None
    tool_use_id: str
    boundary_uuid: str | None
    recorded_at: float | None


class CallerSelection(NamedTuple):
    source_id: str | None
    transcript: Path
    invocation: PrepareInvocation | None


def description_slug(description: str) -> str:
    """Derive a readable, bounded agent name from a description."""
    ascii_text = unicodedata.normalize("NFKD", description).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    useful = [word for word in words if word not in STOP_WORDS]
    chosen = (useful or words)[:5]
    slug = "-".join(chosen)[:40].strip("-")
    return slug or "agent"


def validate_agent_id(agent_id: str) -> str:
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise UsageError(
            "agent id must be 'a' plus 16 lowercase hex digits, optionally preceded "
            "by a 1-63 character ASCII name and '-'"
        )
    return agent_id


def generated_agent_id(slug: str) -> str:
    # Existing Claude Code generated ids use an `a` prefix and a 16-hex suffix.
    bounded = slug[:40].rstrip("-") or "agent"
    return f"a{bounded}-{secrets.token_hex(8)}"


def find_parent_jsonl(
    *,
    parent_jsonl: str | None,
    session_id: str | None,
    projects_root: Path,
) -> tuple[Path, str]:
    if parent_jsonl:
        candidate = Path(parent_jsonl).expanduser().resolve()
        if not candidate.is_file():
            raise UsageError(f"parent transcript does not exist: {candidate}")
        inferred = candidate.stem
        if session_id and session_id != inferred:
            raise UsageError(
                f"--session-id {session_id!r} does not match transcript {inferred!r}"
            )
        return candidate, inferred

    resolved_session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not resolved_session_id:
        raise UsageError(
            "cannot determine the parent session; pass --session-id or --parent-jsonl"
        )
    if "/" in resolved_session_id or resolved_session_id in {".", ".."}:
        raise UsageError(f"invalid session id: {resolved_session_id!r}")

    matches = sorted(projects_root.glob(f"*/{resolved_session_id}.jsonl"))
    if not matches:
        raise UsageError(
            f"no transcript named {resolved_session_id}.jsonl under {projects_root}"
        )
    if len(matches) > 1:
        rendered = "\n  ".join(str(item) for item in matches)
        raise UsageError(
            "the session id exists in more than one project; pass --parent-jsonl:\n"
            f"  {rendered}"
        )
    return matches[0].resolve(), resolved_session_id


def message_tool_use_ids(entry: dict[str, Any]) -> set[str]:
    if entry.get("type") != "assistant":
        return set()
    content = entry.get("message", {}).get("content")
    if not isinstance(content, list):
        return set()
    return {
        block["id"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and isinstance(block.get("id"), str)
    }


def message_tool_result_ids(entry: dict[str, Any]) -> set[str]:
    if entry.get("type") != "user":
        return set()
    content = entry.get("message", {}).get("content")
    if not isinstance(content, list):
        return set()
    return {
        block["tool_use_id"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and isinstance(block.get("tool_use_id"), str)
    }


def read_parent_reference(
    parent_jsonl: Path,
    explicit_uuid: str | None,
) -> tuple[str, int, dict[str, Any]]:
    """Return a usable parent UUID, an informational count, and its record."""
    entries: list[dict[str, Any]] = []
    by_uuid: dict[str, dict[str, Any]] = {}

    with parent_jsonl.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    for line_number, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            # A live writer can leave its final line incomplete for a moment. A malformed
            # earlier line means the parent cannot be read reliably.
            if line_number == len(lines) and not line.endswith("\n"):
                continue
            raise UsageError(
                f"invalid JSON in {parent_jsonl} at line {line_number}: {error}"
            ) from error
        if not isinstance(entry, dict):
            continue
        entry_uuid = entry.get("uuid")
        if not isinstance(entry_uuid, str) or entry.get("isSidechain") is True:
            continue
        entries.append(entry)
        by_uuid[entry_uuid] = entry

    if not entries:
        raise UsageError(f"parent transcript contains no UUID records: {parent_jsonl}")

    if explicit_uuid:
        if explicit_uuid not in by_uuid:
            raise UsageError(
                f"parent UUID {explicit_uuid!r} is not in {parent_jsonl}"
            )
        selected = by_uuid[explicit_uuid]
    else:
        selected = entries[-1]

    selected_uuid = str(selected["uuid"])
    selected_index = next(
        index for index, entry in enumerate(entries) if entry.get("uuid") == selected_uuid
    )
    completed_tool_ids = {
        tool_id
        for entry in entries[: selected_index + 1]
        for tool_id in message_tool_result_ids(entry)
    }
    pending = message_tool_use_ids(selected) - completed_tool_ids
    if pending:
        if explicit_uuid:
            raise UsageError(
                f"parent UUID {explicit_uuid!r} ends at an unfinished tool call"
            )
        # The helper normally runs inside this tool call. Referencing the assistant's
        # unfinished tool_use would leave the new worker with no matching tool_result.
        parent_uuid = selected.get("parentUuid")
        if not isinstance(parent_uuid, str) or parent_uuid not in by_uuid:
            raise UsageError(
                "the newest parent entry is an unfinished tool call and has no usable parent"
            )
        selected = by_uuid[parent_uuid]
        selected_uuid = str(selected["uuid"])
        selected_index = next(
            index for index, entry in enumerate(entries) if entry.get("uuid") == selected_uuid
        )
        completed_tool_ids = {
            tool_id
            for entry in entries[: selected_index + 1]
            for tool_id in message_tool_result_ids(entry)
        }
        if message_tool_use_ids(selected) - completed_tool_ids:
            raise UsageError(
                "the parent before the newest unfinished tool call is also incomplete"
            )

    return selected_uuid, selected_index + 1, selected


def write_bytes_exclusive(path: Path, data: bytes, mode: int = 0o600) -> None:
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        created = True
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
    except BaseException:
        if created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def json_line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def same_inode(path: Path, expected: os.stat_result) -> bool:
    try:
        actual = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)


def publish_agent_files(
    subagents_directory: Path,
    agent_id: str,
    transcript_bytes: bytes,
    metadata: dict[str, Any],
) -> tuple[Path, Path]:
    subagents_directory.mkdir(parents=True, exist_ok=True)
    jsonl_path = subagents_directory / f"agent-{agent_id}.jsonl"
    meta_path = subagents_directory / f"agent-{agent_id}.meta.json"
    lock_path = subagents_directory / ".super-subagent.lock"

    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if jsonl_path.exists() or jsonl_path.is_symlink():
            raise UsageError(f"agent transcript already exists: {jsonl_path}")
        if meta_path.exists() or meta_path.is_symlink():
            raise UsageError(f"agent metadata already exists: {meta_path}")

        nonce = secrets.token_hex(8)
        temporary_meta = subagents_directory / f".{meta_path.name}.{nonce}.tmp"
        temporary_jsonl = subagents_directory / f".{jsonl_path.name}.{nonce}.tmp"
        published: list[tuple[Path, os.stat_result]] = []
        try:
            write_bytes_exclusive(temporary_meta, json_line(metadata))
            write_bytes_exclusive(temporary_jsonl, transcript_bytes)
            for temporary, destination, label in (
                (temporary_meta, meta_path, "metadata"),
                (temporary_jsonl, jsonl_path, "transcript"),
            ):
                temporary_stat = temporary.stat(follow_symlinks=False)
                try:
                    # Hard-linking is an atomic no-replace publication on one filesystem.
                    os.link(temporary, destination)
                except FileExistsError as error:
                    raise UsageError(
                        f"agent {label} already exists: {destination}"
                    ) from error
                published.append((destination, temporary_stat))
            fsync_directory(subagents_directory)
        except BaseException:
            for destination, temporary_stat in reversed(published):
                if same_inode(destination, temporary_stat):
                    try:
                        destination.unlink()
                    except FileNotFoundError:
                        pass
            try:
                fsync_directory(subagents_directory)
            except OSError:
                pass
            raise
        finally:
            # These are private temporary files made by this invocation, not transcripts.
            for temporary in (temporary_meta, temporary_jsonl):
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
    return jsonl_path, meta_path


def fork_model_settings(args: argparse.Namespace) -> tuple[str, bool, str | None]:
    model = args.model.strip()
    if not model:
        raise UsageError("model must not be empty")
    fork_another_model = args.fork_another_model
    if model != "inherit" and not fork_another_model:
        raise UsageError(
            "a non-inherit model requires --fork-another-model; the selected model "
            "cannot reuse the parent's cached prefix"
        )
    if model == "inherit" and fork_another_model:
        raise UsageError("--fork-another-model requires a non-inherit --model value")
    warning = (
        "Forking inherited context onto another model cannot reuse the parent's prompt "
        "cache. The full inherited context may be processed and cached again under the "
        "selected model; repeated model switches repeat that cost."
        if fork_another_model
        else None
    )
    return model, fork_another_model, warning


def agent_identity(args: argparse.Namespace) -> tuple[str, str, str]:
    description = args.description.strip()
    if not description:
        raise UsageError("description must not be empty")
    slug = description_slug(description)
    agent_name = args.name or slug
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", agent_name):
        raise UsageError(
            "agent name must contain only letters, digits, '-' or '_' and be at most 64 characters"
        )
    agent_id = validate_agent_id(args.agent_id) if args.agent_id else generated_agent_id(slug)
    return description, agent_name, agent_id


def spawn_depth(args: argparse.Namespace, source_path: Path | None = None) -> int:
    requested = args.spawn_depth
    if requested is not None:
        if requested < 0:
            raise UsageError("--spawn-depth must be non-negative")
        return requested
    if source_path is None:
        return 1

    metadata_path = source_path.with_name(
        source_path.name.removesuffix(".jsonl") + ".meta.json"
    )
    source_depth = 1
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = None
    if isinstance(metadata, dict):
        value = metadata.get("spawnDepth")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            source_depth = value
    return source_depth + 1


def make_fork_seed(
    agent_id: str,
    session_id: str,
    parent_uuid: str | None,
    template_record: dict[str, Any],
) -> dict[str, Any]:
    seed: dict[str, Any] = {
        "parentUuid": parent_uuid,
        "isSidechain": True,
        "promptId": str(uuid.uuid4()),
        "agentId": agent_id,
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        FORK_BOILERPLATE
                        + "\n\nYour directive is the following user message."
                    ),
                }
            ],
        },
        "uuid": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "userType": "external",
        "entrypoint": "cli",
        "cwd": template_record.get("cwd", os.getcwd()),
        "sessionId": session_id,
    }
    for key in ("version", "gitBranch", "slug"):
        value = template_record.get(key)
        if value is not None:
            seed[key] = value
    return seed


def prepare_main_agent(
    args: argparse.Namespace, invocation: PrepareInvocation | None
) -> dict[str, Any]:
    description, agent_name, agent_id = agent_identity(args)
    model, fork_another_model, cache_warning = fork_model_settings(args)

    projects_root = Path(args.projects_root).expanduser().resolve()
    parent_jsonl, session_id = find_parent_jsonl(
        parent_jsonl=args.parent_jsonl,
        session_id=args.session_id,
        projects_root=projects_root,
    )
    boundary_uuid = args.parent_last_uuid
    if boundary_uuid is None and invocation is not None:
        boundary_uuid = invocation.boundary_uuid
        if boundary_uuid is None:
            raise UsageError("the active main prepare invocation has no parent boundary")
    parent_uuid, context_length, parent_record = read_parent_reference(
        parent_jsonl, boundary_uuid
    )

    session_directory = parent_jsonl.parent / session_id
    subagents_directory = session_directory / "subagents"

    reference = {
        "type": "fork-context-ref",
        "agentId": agent_id,
        "parentSessionId": session_id,
        "parentLastUuid": parent_uuid,
        # Claude Code 2.1.259 records this field but does not read it while hydrating.
        "contextLength": context_length,
    }
    seed = make_fork_seed(agent_id, session_id, None, parent_record)

    # A resumable transcript needs at least one sidechain message. The reference alone
    # is context metadata and getAgentTranscript reports `no_agent_messages` for it.
    transcript_bytes = json_line(reference) + json_line(seed)
    metadata = {
        "agentType": "general-purpose" if fork_another_model else "fork",
        **({} if fork_another_model else {"isFork": True}),
        "description": description,
        "name": agent_name,
        "spawnDepth": spawn_depth(args),
        "model": model,
    }
    jsonl_path, meta_path = publish_agent_files(
        subagents_directory, agent_id, transcript_bytes, metadata
    )

    return {
        "agent_id": agent_id,
        "name": agent_name,
        "description": description,
        "mode": "fork-another-model" if fork_another_model else "fork",
        "model": model,
        **({"cache_warning": cache_warning} if cache_warning else {}),
        "parent_session_id": session_id,
        "parent_last_uuid": parent_uuid,
        "jsonl": str(jsonl_path),
        "meta": str(meta_path),
        "send_message": {"to": agent_id, "message": "<directive>"},
    }


def read_agent_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            if line_number == len(lines) and not line.endswith("\n"):
                continue
            raise UsageError(
                f"invalid JSON in {path} at line {line_number}: {error}"
            ) from error
        if isinstance(record, dict):
            records.append(record)
    return records


SHELL_OPERATORS = {";", "&&", "||", "|", "&"}
SHELL_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
PYTHON_COMMAND_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?")


def shell_command_segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for word in words:
        if word in SHELL_OPERATORS:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(word)
    return [segment for segment in segments if segment]


def segment_invokes_prepare(
    words: list[str],
    description: str | None = None,
    invocation_id: str | None = None,
) -> bool:
    assignments: dict[str, str] = {}

    def record_assignment(word: str) -> None:
        name, value = word.split("=", 1)
        assignments[name] = value

    index = 0
    while index < len(words) and SHELL_ASSIGNMENT_RE.fullmatch(words[index]):
        record_assignment(words[index])
        index += 1
    while index < len(words):
        command = Path(words[index]).name
        if command in {"command", "exec"}:
            index += 1
            continue
        if command == "env":
            index += 1
            while index < len(words) and (
                words[index].startswith("-")
                or SHELL_ASSIGNMENT_RE.fullmatch(words[index])
            ):
                if SHELL_ASSIGNMENT_RE.fullmatch(words[index]):
                    record_assignment(words[index])
                index += 1
            continue
        break
    if index >= len(words):
        return False

    command = Path(words[index]).name
    prepare_index: int | None = None
    if command == "super_subagent.py":
        if index + 1 < len(words) and words[index + 1] == "prepare":
            prepare_index = index + 1
    elif PYTHON_COMMAND_RE.fullmatch(command):
        index += 1
        while index < len(words) and words[index].startswith("-"):
            if words[index] in {"-c", "-m"}:
                return False
            index += 1
        if (
            index + 1 < len(words)
            and Path(words[index]).name == "super_subagent.py"
            and words[index + 1] == "prepare"
        ):
            prepare_index = index + 1
    if prepare_index is None:
        return False
    if invocation_id is not None and assignments.get(CALLER_INVOCATION_ENV) != invocation_id:
        return False
    return description is None or description in words[prepare_index + 1 :]


def command_invokes_prepare(
    command: str,
    description: str | None = None,
    invocation_id: str | None = None,
) -> bool:
    return any(
        segment_invokes_prepare(segment, description, invocation_id)
        for segment in shell_command_segments(command)
    )


def active_prepare_invocations(
    path: Path,
    source_id: str | None,
    description: str,
    invocation_id: str | None = None,
) -> list[PrepareInvocation]:
    try:
        records = read_agent_records(path)
    except (OSError, UsageError):
        return []
    completed = {
        tool_id
        for record in records
        for tool_id in message_tool_result_ids(record)
    }

    def belongs_to_caller(record: dict[str, Any]) -> bool:
        if source_id is None:
            return record.get("isSidechain") is not True
        return (
            record.get("agentId") == source_id
            and record.get("isSidechain") is True
        )

    invocations: list[PrepareInvocation] = []
    for record_index, record in enumerate(records):
        if record.get("type") != "assistant" or not belongs_to_caller(record):
            continue
        assistant_uuid = record.get("uuid")
        if not isinstance(assistant_uuid, str):
            continue
        message = record.get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        response_id = message.get("id") if isinstance(message.get("id"), str) else None
        group_indices = [
            index
            for index, candidate in enumerate(records)
            if candidate.get("type") == "assistant"
            and belongs_to_caller(candidate)
            and (
                candidate.get("message", {}).get("id") == response_id
                if response_id is not None
                else index == record_index
            )
        ]
        group_start = min(group_indices)
        group_end = max(group_indices)
        later_completed_response = any(
            candidate.get("type") == "assistant"
            and belongs_to_caller(candidate)
            and candidate.get("message", {}).get("id") != response_id
            and candidate.get("message", {}).get("stop_reason") is not None
            for candidate in records[group_end + 1 :]
        )
        if later_completed_response:
            continue
        boundary_uuid = records[group_start].get("parentUuid")
        if boundary_uuid is not None and not isinstance(boundary_uuid, str):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            tool_input = block.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if (
                isinstance(tool_id, str)
                and tool_id not in completed
                and block.get("name") == "Bash"
                and isinstance(command, str)
                and command_invokes_prepare(command, description, invocation_id)
            ):
                timestamp = record_timestamp(record)
                invocations.append(
                    PrepareInvocation(
                        source_id=source_id,
                        transcript=path,
                        assistant_uuid=assistant_uuid,
                        response_id=response_id,
                        tool_use_id=tool_id,
                        boundary_uuid=boundary_uuid,
                        recorded_at=(
                            None if timestamp == float("-inf") else timestamp
                        ),
                    )
                )
    return invocations


def agent_id_from_filename(path: Path) -> str:
    name = path.name
    if not name.startswith("agent-") or not name.endswith(".jsonl"):
        raise UsageError(f"not an agent transcript name: {path}")
    return validate_agent_id(name[len("agent-") : -len(".jsonl")])


def agent_transcript_candidates(
    subagents_directory: Path,
) -> list[tuple[str, Path]]:
    paths = [
        *sorted(subagents_directory.glob("agent-*.jsonl")),
        *sorted(subagents_directory.glob("workflows/wf_*/agent-*.jsonl")),
    ]
    candidates: list[tuple[str, Path]] = []
    seen: set[tuple[str, Path]] = set()
    for transcript in paths:
        if not transcript.is_file():
            continue
        try:
            agent_id = agent_id_from_filename(transcript)
        except UsageError:
            continue
        try:
            resolved = transcript.resolve(strict=True)
        except OSError:
            continue
        key = (agent_id, resolved)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((agent_id, transcript))
    return candidates


def select_unique_agent(
    matches: list[tuple[str, Path]], option: str
) -> tuple[str, Path]:
    if len(matches) != 1:
        rendered = ", ".join(f"{agent_id}:{path}" for agent_id, path in matches)
        raise UsageError(
            f"{option} matched {rendered or 'none'}; pass an unambiguous --from-agent-id"
        )
    return matches[0]


def source_agent_from_options(
    args: argparse.Namespace,
    candidates: list[tuple[str, Path]],
) -> tuple[str, Path] | None:
    explicit_id = args.from_agent_id
    environment_id = os.environ.get("CLAUDE_CODE_AGENT_ID") or os.environ.get(
        "CLAUDE_AGENT_ID"
    )
    if explicit_id or environment_id:
        source_id = validate_agent_id(explicit_id or environment_id)
        return select_unique_agent(
            [candidate for candidate in candidates if candidate[0] == source_id],
            "--from-agent-id" if explicit_id else "the agent environment id",
        )
    if args.from_agent_name:
        matches: list[tuple[str, Path]] = []
        for agent_id, transcript in candidates:
            metadata_path = transcript.with_name(f"agent-{agent_id}.meta.json")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(metadata, dict) and metadata.get("name") == args.from_agent_name:
                matches.append((agent_id, transcript))
        return select_unique_agent(matches, f"--from-agent-name {args.from_agent_name!r}")
    return None


def caller_invocation_id(required: bool = False) -> str | None:
    value = os.environ.get(CALLER_INVOCATION_ENV)
    if value is None:
        if required:
            raise UsageError(
                "automatic caller detection requires the invocation id supplied by "
                "the super-subagent skill; otherwise pass --from-main or "
                "--from-agent-id"
            )
        return None
    if not CALLER_INVOCATION_RE.fullmatch(value):
        raise UsageError(
            f"{CALLER_INVOCATION_ENV} must contain 32 lowercase hex digits"
        )
    return value


def discover_prepare_caller(
    args: argparse.Namespace,
    parent_jsonl: Path,
    subagents_directory: Path,
) -> CallerSelection:
    if args.from_main:
        return CallerSelection(None, parent_jsonl, None)

    transcript_candidates = agent_transcript_candidates(subagents_directory)
    selected = source_agent_from_options(args, transcript_candidates)
    if selected is not None:
        source_id, source_path = selected
        source_is_explicit = bool(args.from_agent_id or args.from_agent_name)
        invocation_id = caller_invocation_id(required=not source_is_explicit)
        invocations = active_prepare_invocations(
            source_path, source_id, args.description, invocation_id
        )
        invocation = invocations[-1] if invocations else None
        return CallerSelection(source_id, source_path, invocation)

    invocation_id = caller_invocation_id(required=True)
    timeout = float(args.caller_discovery_timeout)
    if not 0 < timeout <= MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS:
        raise UsageError(
            "--caller-discovery-timeout must be greater than zero and at most "
            f"{MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS:g} seconds"
        )
    deadline = time.monotonic() + timeout
    started_wall = time.time()
    invocations: list[PrepareInvocation] = []
    stable_signature: tuple[tuple[str, str], ...] | None = None
    stable_since: float | None = None
    while True:
        found = active_prepare_invocations(
            parent_jsonl, None, args.description, invocation_id
        )
        for source_id, transcript in agent_transcript_candidates(subagents_directory):
            found.extend(
                active_prepare_invocations(
                    transcript, source_id, args.description, invocation_id
                )
            )
        invocations = [
            invocation
            for invocation in found
            if invocation.recorded_at is not None
            and invocation.recorded_at
            >= started_wall - CALLER_INVOCATION_MAX_AGE_SECONDS
        ]
        now = time.monotonic()
        signature = tuple(
            sorted(
                (invocation.source_id or "", invocation.tool_use_id)
                for invocation in invocations
            )
        )
        if invocations:
            if signature != stable_signature:
                stable_signature = signature
                stable_since = now
            if (
                stable_since is not None
                and now - stable_since >= CALLER_DISCOVERY_SETTLE_SECONDS
            ):
                break
        else:
            stable_signature = None
            stable_since = None
        remaining = deadline - now
        if remaining <= 0:
            break
        # The Bash child can begin before the transcript writer publishes the
        # assistant tool_use that identifies its caller.
        time.sleep(min(CALLER_DISCOVERY_POLL_INTERVAL_SECONDS, remaining))

    if len(invocations) != 1:
        labels = [invocation.source_id or "main" for invocation in invocations]
        rendered = ", ".join(labels) if labels else "none"
        raise UsageError(
            "could not identify one caller from its active prepare invocation "
            f"(candidates: {rendered}); pass --from-main or --from-agent-id"
        )
    invocation = invocations[0]
    return CallerSelection(invocation.source_id, invocation.transcript, invocation)


def entry_tool_use_ids(entry: dict[str, Any]) -> set[str]:
    return message_tool_use_ids(entry)


def recover_parallel_tool_results(
    sidechain: list[dict[str, Any]],
    base_chain: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    visited = {str(record["uuid"]) for record in base_chain}
    response_order: list[str] = []
    response_anchor: dict[str, str] = {}
    assistants_by_response: dict[str, list[dict[str, Any]]] = {}
    results_by_parent: dict[str, list[dict[str, Any]]] = {}

    for record in sidechain:
        if record.get("type") == "assistant":
            response_id = record.get("message", {}).get("id")
            if isinstance(response_id, str):
                assistants_by_response.setdefault(response_id, []).append(record)
        elif record.get("type") == "user" and message_tool_result_ids(record):
            parent_uuid = record.get("parentUuid")
            if isinstance(parent_uuid, str):
                results_by_parent.setdefault(parent_uuid, []).append(record)

    for record in base_chain:
        if record.get("type") != "assistant":
            continue
        response_id = record.get("message", {}).get("id")
        if not isinstance(response_id, str):
            continue
        if response_id not in response_anchor:
            response_order.append(response_id)
        response_anchor[response_id] = str(record["uuid"])

    additions: dict[str, list[dict[str, Any]]] = {}
    for response_id in response_order:
        missing_assistants = [
            record
            for record in assistants_by_response.get(response_id, [])
            if str(record["uuid"]) not in visited
        ]
        missing_results: list[dict[str, Any]] = []
        for assistant in assistants_by_response.get(response_id, []):
            for result in results_by_parent.get(str(assistant["uuid"]), []):
                if str(result["uuid"]) not in visited:
                    missing_results.append(result)
        recovered = missing_assistants + missing_results
        recovered.sort(key=record_timestamp)
        if recovered:
            additions[response_anchor[response_id]] = recovered
            visited.update(str(record["uuid"]) for record in recovered)

    expanded: list[dict[str, Any]] = []
    for record in base_chain:
        expanded.append(record)
        expanded.extend(additions.get(str(record["uuid"]), []))
    return expanded


def record_timestamp(record: dict[str, Any]) -> float:
    value = record.get("timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float("-inf")


def response_group_key(record: dict[str, Any]) -> str:
    message = record.get("message")
    if isinstance(message, dict) and isinstance(message.get("id"), str):
        return f"response:{message['id']}"
    return f"record:{record.get('uuid')}"


def active_sidechain_chain(
    records: list[dict[str, Any]],
    source_id: str,
    invocation: PrepareInvocation | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int, list[str]]:
    references = [
        record
        for record in records
        if record.get("type") == "fork-context-ref"
        and record.get("agentId") == source_id
    ]
    reference = references[-1] if references else None

    records_with_uuid = [
        record for record in records if isinstance(record.get("uuid"), str)
    ]
    owned_records = [
        record
        for record in records_with_uuid
        if record.get("agentId") == source_id
        and record.get("isSidechain") is True
    ]
    if not owned_records:
        raise UsageError(f"source agent {source_id} has no sidechain messages")

    by_uuid = {str(record["uuid"]): record for record in records_with_uuid}
    if invocation is not None:
        leaf = by_uuid.get(invocation.assistant_uuid)
        if leaf is None:
            raise UsageError(
                f"active prepare invocation {invocation.assistant_uuid!r} is missing "
                f"from source agent {source_id}"
            )
    else:
        parent_uuids = {
            str(record["parentUuid"])
            for record in records_with_uuid
            if isinstance(record.get("parentUuid"), str)
        }
        leaves = [
            record
            for record in owned_records
            if record["uuid"] not in parent_uuids
            and not (
                record.get("type") == "system"
                and record.get("subtype") == "compact_boundary"
            )
        ]
        if not leaves:
            raise UsageError(f"source agent {source_id} has no sidechain leaf")
        positions = {
            str(record["uuid"]): index for index, record in enumerate(records_with_uuid)
        }
        leaf = max(leaves, key=lambda record: positions[str(record["uuid"])])

    reverse_chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = leaf
    while current is not None:
        current_uuid = str(current["uuid"])
        if current_uuid in seen:
            raise UsageError(f"source agent {source_id} has a parentUuid cycle")
        seen.add(current_uuid)
        reverse_chain.append(current)
        parent_uuid = current.get("parentUuid")
        if parent_uuid is None:
            logical_parent = current.get("logicalParentUuid")
            if (
                current.get("type") == "system"
                and current.get("subtype") == "compact_boundary"
                and isinstance(logical_parent, str)
            ):
                if logical_parent not in by_uuid:
                    raise UsageError(
                        f"source agent {source_id} compacted across a missing "
                        f"logicalParentUuid {logical_parent!r}"
                    )
                current = by_uuid[logical_parent]
                continue
            break
        if not isinstance(parent_uuid, str) or parent_uuid not in by_uuid:
            raise UsageError(
                f"source agent {source_id} has a missing parentUuid {parent_uuid!r}"
            )
        current = by_uuid[parent_uuid]
    base_chain = list(reversed(reverse_chain))
    chain = recover_parallel_tool_results(records_with_uuid, base_chain)

    cut_index = len(chain)
    active_tool_ids: set[str] = set()
    if invocation is not None:
        matching = [
            index
            for index, record in enumerate(chain)
            if record.get("type") == "assistant"
            and (
                record.get("message", {}).get("id") == invocation.response_id
                if invocation.response_id is not None
                else record.get("uuid") == invocation.assistant_uuid
            )
        ]
        if not matching:
            raise UsageError("the active prepare response is absent from its sidechain")
        cut_index = min(matching)
        active_tool_ids = {
            tool_id
            for record in chain[cut_index:]
            if record.get("type") == "assistant"
            and (
                record.get("message", {}).get("id") == invocation.response_id
                if invocation.response_id is not None
                else record.get("uuid") == invocation.assistant_uuid
            )
            for tool_id in entry_tool_use_ids(record)
        }
    elif chain[-1].get("type") == "assistant" and chain[-1].get(
        "message", {}
    ).get("stop_reason") is None:
        active_key = response_group_key(chain[-1])
        cut_index = next(
            index
            for index, record in enumerate(chain)
            if response_group_key(record) == active_key
        )

    prefix = chain[:cut_index]
    if not prefix:
        raise UsageError(f"source agent {source_id} has no completed sidechain prefix")

    tool_results = {
        tool_id
        for record in prefix
        for tool_id in message_tool_result_ids(record)
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in prefix:
        if record.get("type") == "assistant":
            groups.setdefault(response_group_key(record), []).append(record)
    removed_uuids: set[str] = set()
    removed_tool_ids: set[str] = set()
    for group in groups.values():
        group_tools = {
            tool_id for record in group for tool_id in entry_tool_use_ids(record)
        }
        if group_tools - tool_results:
            removed_tool_ids.update(group_tools)
            removed_uuids.update(str(record["uuid"]) for record in group)
    for record in prefix:
        if message_tool_result_ids(record) & removed_tool_ids:
            removed_uuids.add(str(record["uuid"]))
        source_assistant = record.get("sourceToolAssistantUUID")
        if isinstance(source_assistant, str) and source_assistant in removed_uuids:
            removed_uuids.add(str(record["uuid"]))

    def retained_parent(parent_uuid: Any) -> str | None:
        visited: set[str] = set()
        while isinstance(parent_uuid, str) and parent_uuid in removed_uuids:
            if parent_uuid in visited:
                raise UsageError(f"source agent {source_id} has a removed-parent cycle")
            visited.add(parent_uuid)
            parent_uuid = by_uuid[parent_uuid].get("parentUuid")
        return parent_uuid if isinstance(parent_uuid, str) else None

    completed: list[dict[str, Any]] = []
    for record in prefix:
        if str(record["uuid"]) in removed_uuids:
            continue
        clone = copy.deepcopy(record)
        clone["parentUuid"] = retained_parent(record.get("parentUuid"))
        logical_parent = record.get("logicalParentUuid")
        if isinstance(logical_parent, str) and logical_parent in removed_uuids:
            replacement = retained_parent(logical_parent)
            if replacement is None:
                clone.pop("logicalParentUuid", None)
            else:
                clone["logicalParentUuid"] = replacement
        completed.append(clone)

    if not completed:
        raise UsageError(f"source agent {source_id} has no completed sidechain prefix")
    remaining_uses = {
        tool_id
        for record in completed
        for tool_id in entry_tool_use_ids(record)
    }
    remaining_results = {
        tool_id
        for record in completed
        for tool_id in message_tool_result_ids(record)
    }
    if remaining_uses - remaining_results:
        raise UsageError(
            f"source agent {source_id} still has unmatched tool_use records after trimming"
        )
    dropped = len(chain) - len(completed)
    return reference, completed, dropped, sorted(removed_tool_ids | active_tool_ids)


def remap_compact_metadata(
    clone: dict[str, Any],
    uuid_map: dict[str, str],
    source_uuids: set[str],
) -> None:
    compact = clone.get("compactMetadata")
    if not isinstance(compact, dict):
        return

    def remap_value(container: dict[str, Any], key: str) -> None:
        value = container.get(key)
        if not isinstance(value, str):
            return
        if value in uuid_map:
            container[key] = uuid_map[value]
        elif value in source_uuids:
            raise UsageError(
                f"compactMetadata.{key} references an omitted source UUID {value!r}"
            )

    segment = compact.get("preservedSegment")
    if isinstance(segment, dict):
        for key in ("headUuid", "anchorUuid", "tailUuid"):
            remap_value(segment, key)

    messages = compact.get("preservedMessages")
    if isinstance(messages, dict):
        remap_value(messages, "anchorUuid")
        for key in ("uuids", "allUuids"):
            values = messages.get(key)
            if not isinstance(values, list):
                continue
            remapped: list[Any] = []
            for value in values:
                if isinstance(value, str) and value in uuid_map:
                    remapped.append(uuid_map[value])
                elif isinstance(value, str) and value in source_uuids:
                    raise UsageError(
                        f"compactMetadata.{key} references an omitted source UUID "
                        f"{value!r}"
                    )
                else:
                    remapped.append(value)
            messages[key] = remapped


def remap_sidechain(
    records: list[dict[str, Any]],
    target_id: str,
    source_uuids: set[str],
) -> list[dict[str, Any]]:
    uuid_map = {str(record["uuid"]): str(uuid.uuid4()) for record in records}
    prompt_map: dict[str, str] = {}
    remapped: list[dict[str, Any]] = []
    for record in records:
        clone = copy.deepcopy(record)
        old_uuid = str(record["uuid"])
        clone["uuid"] = uuid_map[old_uuid]
        parent_uuid = record.get("parentUuid")
        clone["parentUuid"] = (
            uuid_map[parent_uuid] if isinstance(parent_uuid, str) else None
        )
        logical_parent = record.get("logicalParentUuid")
        if isinstance(logical_parent, str) and logical_parent in uuid_map:
            clone["logicalParentUuid"] = uuid_map[logical_parent]
        source_tool_assistant = record.get("sourceToolAssistantUUID")
        if isinstance(source_tool_assistant, str) and source_tool_assistant in uuid_map:
            clone["sourceToolAssistantUUID"] = uuid_map[source_tool_assistant]
        prompt_id = record.get("promptId")
        if isinstance(prompt_id, str):
            clone["promptId"] = prompt_map.setdefault(prompt_id, str(uuid.uuid4()))
        clone["agentId"] = target_id
        clone["isSidechain"] = True
        remap_compact_metadata(clone, uuid_map, source_uuids)
        remapped.append(clone)
    return remapped


def make_completed_tail_record(
    source_record: dict[str, Any],
    parent_uuid: str,
    target_id: str,
    session_id: str,
) -> dict[str, Any]:
    source_message = source_record.get("message")
    source_model = (
        source_message.get("model") if isinstance(source_message, dict) else None
    )
    record: dict[str, Any] = {
        "parentUuid": parent_uuid,
        "isSidechain": True,
        "agentId": target_id,
        "type": "assistant",
        "message": {
            "id": "msg_" + secrets.token_hex(12),
            "type": "message",
            "role": "assistant",
            "model": source_model or "inherit",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "content": [
                {
                    "type": "text",
                    "text": (
                        "The source transcript snapshot ends here. The next user "
                        "message is the new directive."
                    ),
                }
            ],
            "stop_reason": "end_turn",
        },
        "uuid": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "userType": "external",
        "entrypoint": "cli",
        "cwd": source_record.get("cwd", os.getcwd()),
        "sessionId": session_id,
    }
    for key in ("version", "gitBranch", "slug"):
        value = source_record.get(key)
        if value is not None:
            record[key] = value
    return record


def prepare_worker_agent(
    args: argparse.Namespace,
    source_id: str,
    source_path: Path,
    invocation: PrepareInvocation | None,
) -> dict[str, Any]:
    description, agent_name, agent_id = agent_identity(args)
    model, fork_another_model, cache_warning = fork_model_settings(args)
    projects_root = Path(args.projects_root).expanduser().resolve()
    parent_jsonl, session_id = find_parent_jsonl(
        parent_jsonl=args.parent_jsonl,
        session_id=args.session_id,
        projects_root=projects_root,
    )
    subagents_directory = parent_jsonl.parent / session_id / "subagents"
    if source_id == agent_id:
        raise UsageError("source and target agent ids must differ")

    source_records = read_agent_records(source_path)
    source_reference, completed, dropped_count, unresolved = active_sidechain_chain(
        source_records, source_id, invocation
    )
    source_uuids = {
        str(record["uuid"])
        for record in source_records
        if isinstance(record.get("uuid"), str)
    }
    copied = remap_sidechain(completed, agent_id, source_uuids)
    reference = copy.deepcopy(source_reference) if source_reference is not None else None
    if reference is not None:
        reference["agentId"] = agent_id
    last_message = copied[-1].get("message")
    has_completed_assistant_tail = (
        copied[-1].get("type") == "assistant"
        and isinstance(last_message, dict)
        and last_message.get("stop_reason") is not None
    )
    boundary_records: list[dict[str, Any]] = []
    if not has_completed_assistant_tail:
        boundary_records.append(
            make_completed_tail_record(
                completed[-1], str(copied[-1]["uuid"]), agent_id, session_id
            )
        )
    target_records = copied + boundary_records
    if reference is None:
        target_records.append(
            make_fork_seed(
                agent_id,
                session_id,
                str(target_records[-1]["uuid"]),
                completed[-1],
            )
        )
    transcript_bytes = (
        (json_line(reference) if reference is not None else b"")
        + b"".join(json_line(record) for record in target_records)
    )
    metadata = {
        "agentType": "general-purpose" if fork_another_model else "fork",
        **({} if fork_another_model else {"isFork": True}),
        "description": description,
        "name": agent_name,
        "spawnDepth": spawn_depth(args, source_path),
        "model": model,
        "parentAgentId": source_id,
    }
    jsonl_path, meta_path = publish_agent_files(
        subagents_directory, agent_id, transcript_bytes, metadata
    )
    return {
        "agent_id": agent_id,
        "name": agent_name,
        "description": description,
        "mode": "fork-another-model" if fork_another_model else "fork",
        "model": model,
        **({"cache_warning": cache_warning} if cache_warning else {}),
        "source_agent_id": source_id,
        "copied_sidechain_records": len(copied),
        "snapshot_boundary_records": len(boundary_records),
        "dropped_active_records": dropped_count,
        "dropped_unmatched_tool_use_ids": unresolved,
        "parent_session_id": session_id,
        "parent_last_uuid": (
            reference.get("parentLastUuid") if reference is not None else None
        ),
        "jsonl": str(jsonl_path),
        "meta": str(meta_path),
        "send_message": {"to": agent_id, "message": "<directive>"},
    }


def prepare_agent(args: argparse.Namespace) -> dict[str, Any]:
    projects_root = Path(args.projects_root).expanduser().resolve()
    parent_jsonl, session_id = find_parent_jsonl(
        parent_jsonl=args.parent_jsonl,
        session_id=args.session_id,
        projects_root=projects_root,
    )
    subagents_directory = parent_jsonl.parent / session_id / "subagents"
    selection = discover_prepare_caller(args, parent_jsonl, subagents_directory)
    if selection.source_id is None:
        return prepare_main_agent(args, selection.invocation)
    if args.parent_last_uuid:
        raise UsageError("--parent-last-uuid is valid only when preparing from main")
    return prepare_worker_agent(
        args,
        selection.source_id,
        selection.transcript,
        selection.invocation,
    )


def workflow_directory(
    workflow: str,
    subagents_directory: Path,
) -> Path:
    workflow_root = (subagents_directory / "workflows").resolve()
    supplied = Path(workflow).expanduser()
    if supplied.is_absolute() or "/" in workflow:
        candidate = supplied.resolve()
    else:
        if not WORKFLOW_ID_RE.fullmatch(workflow):
            raise UsageError(
                "workflow must be a wf_* run id or a path to its agent directory"
            )
        candidate = (workflow_root / workflow).resolve()
    if not candidate.is_dir():
        raise UsageError(f"workflow agent directory does not exist: {candidate}")
    if candidate.parent != workflow_root or not WORKFLOW_ID_RE.fullmatch(candidate.name):
        raise UsageError(
            "workflow agent directory must belong to the selected parent session: "
            f"{candidate}"
        )
    return candidate


def source_agent_ids(source_directory: Path) -> list[str]:
    result = []
    for transcript in sorted(source_directory.glob("agent-*.jsonl")):
        agent_id = transcript.name[len("agent-") : -len(".jsonl")]
        result.append(validate_agent_id(agent_id))
    if not result:
        raise UsageError(f"workflow has no agent transcripts: {source_directory}")
    return result


def main_transcript_uuids(parent_jsonl: Path) -> set[str]:
    return {
        str(record["uuid"])
        for record in read_agent_records(parent_jsonl)
        if isinstance(record.get("uuid"), str)
        and record.get("isSidechain") is not True
    }


def validate_workflow_agent(
    source_directory: Path,
    agent_id: str,
    session_id: str,
    parent_uuids: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    transcript_path = source_directory / f"agent-{agent_id}.jsonl"
    metadata_path = source_directory / f"agent-{agent_id}.meta.json"
    if not metadata_path.is_file():
        raise UsageError(f"workflow agent has no metadata file: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UsageError(f"cannot read workflow metadata {metadata_path}: {error}") from error
    if not isinstance(metadata, dict):
        raise UsageError(f"workflow metadata is not a JSON object: {metadata_path}")

    records = read_agent_records(transcript_path)
    sidechain_records = [
        record
        for record in records
        if record.get("agentId") == agent_id
        and record.get("isSidechain") is True
        and isinstance(record.get("uuid"), str)
        and record_timestamp(record) != float("-inf")
    ]
    if not sidechain_records:
        raise UsageError(
            "workflow agent transcript has no exact-ID sidechain record with a "
            f"parseable timestamp: {transcript_path}"
        )

    latest = max(sidechain_records, key=record_timestamp)
    records_by_uuid = {
        str(record["uuid"]): record
        for record in records
        if isinstance(record.get("uuid"), str)
    }
    current = latest
    visited: set[str] = set()
    while isinstance(current.get("parentUuid"), str):
        current_uuid = str(current["uuid"])
        if current_uuid in visited:
            raise UsageError(
                f"workflow agent parent chain contains a cycle: {transcript_path}"
            )
        visited.add(current_uuid)
        parent_uuid = str(current["parentUuid"])
        parent = records_by_uuid.get(parent_uuid)
        if parent is None:
            raise UsageError(
                "workflow agent's latest timestamped sidechain record has a missing "
                f"parent UUID: {transcript_path}"
            )
        current = parent

    references = [
        record for record in records if record.get("type") == "fork-context-ref"
    ]
    for reference in references:
        if reference.get("agentId") != agent_id:
            raise UsageError(
                f"workflow fork reference has the wrong agent id in {transcript_path}"
            )
        if reference.get("parentSessionId") != session_id:
            raise UsageError(
                f"workflow fork belongs to another parent session: {transcript_path}"
            )
        parent_uuid = reference.get("parentLastUuid")
        if not isinstance(parent_uuid, str) or parent_uuid not in parent_uuids:
            raise UsageError(
                f"workflow fork references a missing parent UUID in {transcript_path}"
            )

    return metadata, records


def same_symlink(destination: Path, source: Path) -> bool:
    return destination.is_symlink() and destination.resolve() == source.resolve()


def expected_override_metadata(metadata: dict[str, Any], model: str) -> bytes:
    overridden = copy.deepcopy(metadata)
    overridden["model"] = model
    if overridden.get("isFork") is True or overridden.get("agentType") == "fork":
        overridden.pop("isFork", None)
        overridden["agentType"] = "general-purpose"
    return json_line(overridden)


def regular_file_has_bytes(path: Path, expected: bytes) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        actual = path.read_bytes()
    except OSError:
        return False
    if actual == expected:
        return True
    try:
        return json.loads(actual) == json.loads(expected)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def link_workflow_agents(args: argparse.Namespace) -> dict[str, Any]:
    model_override = args.model.strip() if args.model is not None else None
    if args.model is not None and not model_override:
        raise UsageError("model override must not be empty")

    projects_root = Path(args.projects_root).expanduser().resolve()
    parent_jsonl, session_id = find_parent_jsonl(
        parent_jsonl=args.parent_jsonl,
        session_id=args.session_id,
        projects_root=projects_root,
    )
    subagents_directory = parent_jsonl.parent / session_id / "subagents"
    subagents_directory.mkdir(parents=True, exist_ok=True)
    source_directory = workflow_directory(args.workflow, subagents_directory)
    parent_uuids = main_transcript_uuids(parent_jsonl)

    available_ids = source_agent_ids(source_directory)
    selected_ids = args.agent_id or available_ids
    unknown = sorted(set(selected_ids) - set(available_ids))
    if unknown:
        raise UsageError(
            "workflow does not contain: " + ", ".join(unknown)
        )

    operations: list[dict[str, Any]] = []
    for agent_id in selected_ids:
        source_jsonl = source_directory / f"agent-{agent_id}.jsonl"
        source_meta = source_directory / f"agent-{agent_id}.meta.json"
        source_metadata, _ = validate_workflow_agent(
            source_directory, agent_id, session_id, parent_uuids
        )
        destination_jsonl = subagents_directory / source_jsonl.name
        destination_meta = subagents_directory / source_meta.name
        override_bytes = (
            expected_override_metadata(source_metadata, model_override)
            if model_override is not None
            else None
        )
        operations.append(
            {
                "agent_id": agent_id,
                "source_jsonl": source_jsonl,
                "source_meta": source_meta,
                "destination_jsonl": destination_jsonl,
                "destination_meta": destination_meta,
                "override_bytes": override_bytes,
            }
        )

    lock_path = subagents_directory / ".super-subagent.lock"
    linked: list[dict[str, Any]] = []
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

        # Check every destination before creating any links.
        for operation in operations:
            destination_jsonl = operation["destination_jsonl"]
            destination_meta = operation["destination_meta"]
            source_jsonl = operation["source_jsonl"]
            source_meta = operation["source_meta"]
            override_bytes = operation["override_bytes"]

            if (destination_jsonl.exists() or destination_jsonl.is_symlink()) and not same_symlink(
                destination_jsonl, source_jsonl
            ):
                raise UsageError(f"destination already differs: {destination_jsonl}")

            if destination_meta.exists() or destination_meta.is_symlink():
                metadata_matches = (
                    regular_file_has_bytes(destination_meta, override_bytes)
                    if override_bytes is not None
                    else same_symlink(destination_meta, source_meta)
                )
                if not metadata_matches:
                    raise UsageError(f"destination already differs: {destination_meta}")

        created_paths: list[Path] = []
        try:
            for operation in operations:
                destination_jsonl = operation["destination_jsonl"]
                destination_meta = operation["destination_meta"]
                source_jsonl = operation["source_jsonl"]
                source_meta = operation["source_meta"]
                override_bytes = operation["override_bytes"]

                if not (destination_meta.exists() or destination_meta.is_symlink()):
                    if override_bytes is None:
                        destination_meta.symlink_to(
                            os.path.relpath(source_meta, destination_meta.parent)
                        )
                    else:
                        write_bytes_exclusive(destination_meta, override_bytes)
                    created_paths.append(destination_meta)
                if not (destination_jsonl.exists() or destination_jsonl.is_symlink()):
                    destination_jsonl.symlink_to(
                        os.path.relpath(source_jsonl, destination_jsonl.parent)
                    )
                    created_paths.append(destination_jsonl)

                linked.append(
                    {
                        "agent_id": operation["agent_id"],
                        "jsonl": str(destination_jsonl),
                        "meta": str(destination_meta),
                        "model_override": model_override,
                        "send_message": {
                            "to": operation["agent_id"],
                            "message": "<continuation directive>",
                        },
                    }
                )
            fsync_directory(subagents_directory)
        except BaseException:
            for destination in reversed(created_paths):
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            fsync_directory(subagents_directory)
            raise

    return {
        "workflow": source_directory.name,
        "parent_session_id": session_id,
        "agents": linked,
    }


def add_parent_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--session-id",
        help="parent Claude Code session id; defaults to CLAUDE_CODE_SESSION_ID",
    )
    parser.add_argument(
        "--parent-jsonl",
        help="explicit parent session transcript; resolves ambiguous session ids",
    )
    parser.add_argument(
        "--projects-root",
        default=str(Path.home() / ".claude" / "projects"),
        help="Claude Code projects root, primarily for tests",
    )


def add_fork_creation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("description", help="short description used to generate the agent name")
    parser.add_argument(
        "--model",
        default="inherit",
        help="model id; non-inherit values require --fork-another-model",
    )
    parser.add_argument(
        "--fork-another-model",
        action="store_true",
        help=(
            "acknowledge that another model cannot reuse the parent's cached prefix "
            "and may process and cache it again"
        ),
    )
    parser.add_argument("--name", help="override the generated agent name")
    parser.add_argument("--agent-id", help="use an explicit agent id instead of generating one")
    parser.add_argument(
        "--spawn-depth",
        type=int,
        default=None,
        help="metadata spawn depth; defaults to 1 from main or parent depth plus 1",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare fork agents and resume failed workflow agents through SendMessage."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="prepare a child from the calling main session or worker",
    )
    add_fork_creation_options(prepare)
    source = prepare.add_mutually_exclusive_group()
    source.add_argument(
        "--from-main",
        action="store_true",
        help="fork the main conversation instead of detecting the caller",
    )
    source.add_argument("--from-agent-id", help="exact source agent id")
    source.add_argument(
        "--from-agent-name",
        help="source metadata name; it must match exactly one agent",
    )
    prepare.add_argument(
        "--caller-discovery-timeout",
        type=float,
        default=DEFAULT_CALLER_DISCOVERY_TIMEOUT_SECONDS,
        help=(
            "seconds to wait for the caller's active prepare invocation; "
            f"default {DEFAULT_CALLER_DISCOVERY_TIMEOUT_SECONDS:g}, maximum "
            f"{MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS:g}"
        ),
    )
    prepare.add_argument(
        "--parent-last-uuid",
        help="explicit main transcript UUID; valid only when preparing from main",
    )
    add_parent_options(prepare)
    prepare.set_defaults(handler=prepare_agent)

    link = subparsers.add_parser(
        "link-workflow",
        help="make workflow agent transcripts visible to SendMessage",
    )
    link.add_argument("workflow", help="wf_* run id or path to its agent directory")
    link.add_argument(
        "--agent-id",
        action="append",
        help="link only this agent id; repeat to select several",
    )
    link.add_argument(
        "--model",
        help="write a top-level metadata copy with this model instead of symlinking metadata",
    )
    add_parent_options(link)
    link.set_defaults(handler=link_workflow_agents)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    requested_depth = getattr(args, "spawn_depth", None)
    if requested_depth is not None and requested_depth < 0:
        parser.error("--spawn-depth must be non-negative")
    try:
        result = args.handler(args)
    except UsageError as error:
        print(f"super-subagent: {error}", file=sys.stderr)
        return 2
    if warning := result.get("cache_warning"):
        print(f"super-subagent: WARNING: {warning}", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
