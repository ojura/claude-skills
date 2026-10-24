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


class WorkerSnapshot(NamedTuple):
    records: list[dict[str, Any]]
    boundary_uuid: str | None
    template_record: dict[str, Any]
    dropped_records: int
    dropped_tool_ids: list[str]


class PrepareInvocation(NamedTuple):
    source_id: str | None
    transcript: Path
    assistant_uuid: str
    response_id: str | None
    tool_use_id: str
    boundary_uuid: str | None
    recorded_at: float | None
    template_record: dict[str, Any] | None = None
    source_bytes: bytes | None = None
    response_offset: int = 0


class CallerSelection(NamedTuple):
    source_id: str | None
    transcript: Path
    invocation: PrepareInvocation | None
    worker_snapshot: WorkerSnapshot | None = None
    main_reference: tuple[str, dict[str, Any]] | None = None


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
    message = entry.get("message")
    if not isinstance(message, dict):
        return set()
    content = message.get("content")
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
    message = entry.get("message")
    if not isinstance(message, dict):
        return set()
    content = message.get("content")
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
) -> tuple[str, dict[str, Any]]:
    """Read the requested boundary, or the last main record for a manual fork."""
    data = parent_jsonl.read_bytes()

    def find_uuid(value: str) -> tuple[int, dict[str, Any]]:
        matches = [
            (offset, record) for offset, record in matching_records(data, value, parent_jsonl)
            if record.get("uuid") == value and record.get("isSidechain") is not True
        ]
        if not matches:
            raise UsageError(f"parent UUID {value!r} is not in {parent_jsonl}")
        return matches[-1]

    if explicit_uuid is not None:
        offset, selected = find_uuid(explicit_uuid)
    else:
        end = len(data)
        selected = None
        while end > 0:
            start = data.rfind(b"\n", 0, end - 1) + 1
            line = data[start:end]
            if b'"uuid"' in line:
                records = decode_transcript_records(line, parent_jsonl, data.count(b"\n", 0, start) + 1)
                if records and isinstance(records[0].get("uuid"), str) and records[0].get("isSidechain") is not True:
                    offset, selected = start, records[0]
                    break
            end = start
        if selected is None:
            raise UsageError(f"parent transcript contains no UUID records: {parent_jsonl}")

    def pending(record: dict[str, Any], position: int) -> bool:
        for tool_id in message_tool_use_ids(record):
            if not any(
                result_offset <= position and result.get("isSidechain") is not True
                and tool_id in message_tool_result_ids(result)
                for result_offset, result in matching_records(data, tool_id, parent_jsonl)
            ):
                return True
        return False

    if pending(selected, offset):
        if explicit_uuid is not None:
            raise UsageError(f"parent UUID {explicit_uuid!r} ends at an unfinished tool call")
        parent_uuid = selected.get("parentUuid")
        if not isinstance(parent_uuid, str):
            raise UsageError("the newest parent entry is an unfinished tool call and has no usable parent")
        offset, selected = find_uuid(parent_uuid)
        if pending(selected, offset):
            raise UsageError("the parent before the newest unfinished tool call is also incomplete")
    return str(selected["uuid"]), selected


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
    args: argparse.Namespace, selection: CallerSelection
) -> dict[str, Any]:
    description, agent_name, agent_id = agent_identity(args)
    model, fork_another_model, cache_warning = fork_model_settings(args)

    projects_root = Path(args.projects_root).expanduser().resolve()
    parent_jsonl, session_id = find_parent_jsonl(
        parent_jsonl=args.parent_jsonl,
        session_id=args.session_id,
        projects_root=projects_root,
    )
    if selection.main_reference is not None:
        parent_uuid, parent_record = selection.main_reference
    elif selection.invocation is not None:
        parent_uuid = selection.invocation.boundary_uuid
        if parent_uuid is None:
            raise UsageError("the active main prepare invocation has no parent boundary")
        parent_record = selection.invocation.template_record or {}
    else:
        raise UsageError("caller discovery did not provide the main boundary")

    session_directory = parent_jsonl.parent / session_id
    subagents_directory = session_directory / "subagents"

    reference = {
        "type": "fork-context-ref",
        "agentId": agent_id,
        "parentSessionId": session_id,
        "parentLastUuid": parent_uuid,
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


def decode_transcript_records(
    data: bytes, path: Path, start_line: int = 1,
) -> list[dict[str, Any]]:
    lines = data.decode("utf-8", errors="replace").split("\n")
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip("\x00 \t\r"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1 and not data.endswith(b"\n"):
                continue
            raise UsageError(
                f"invalid JSON in {path} at line {start_line + index}: {error}"
            ) from error
        if isinstance(record, dict):
            records.append(record)
    return records


def read_transcript_records(path: Path) -> list[dict[str, Any]]:
    return decode_transcript_records(path.read_bytes(), path)


def matching_records(
    data: bytes, token: str | re.Pattern[bytes], path: Path, start: int = 0,
) -> Iterable[tuple[int, dict[str, Any]]]:
    """Decode only rows containing the lookup token; a match is not a caller."""
    if isinstance(token, str) and not token:
        raise UsageError(f"empty lookup identifier for {path}")
    pattern = re.compile(re.escape(token.encode())) if isinstance(token, str) else token
    position = start
    while position < len(data) and (match := pattern.search(data, position)):
        offset = data.rfind(b"\n", 0, match.start()) + 1
        end = data.find(b"\n", match.end())
        end = len(data) if end < 0 else end + 1
        for record in decode_transcript_records(
            data[offset:end], path, data.count(b"\n", 0, offset) + 1
        ):
            yield offset, record
        position = end


SHELL_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
PYTHON_COMMAND_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?")


def shell_command_segments(command: str) -> list[list[str]]:
    # Keep quotes until command separators and shell assignments are identified.
    # shlex decodes each word afterwards; quoted operators remain arguments.
    segments: list[list[str]] = [[]]
    word: list[str] = []
    quote: str | None = None
    index = 0

    def finish_word() -> None:
        if word:
            segments[-1].append("".join(word))
            word.clear()

    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            if index + 1 == len(command):
                return []
            if command[index + 1] != "\n":
                word.extend(command[index:index + 2])
            index += 2
            continue
        if quote is not None:
            word.append(char)
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            word.append(char)
        elif char == "#" and not word:
            newline = command.find("\n", index)
            index = len(command) if newline < 0 else newline
            continue
        elif char in "()`" or command.startswith(("<<", "$("), index):
            # Compound shell programs are not inferred from text inside them.
            return []
        elif char in "<>" or command.startswith("&>", index):
            finish_word()
            operator = next(
                (value for value in ("&>>", "&>", ">&", "<&", ">>", ">|", "<>")
                 if command.startswith(value, index)), char
            )
            segments[-1].append(operator)
            index += len(operator)
            continue
        elif char in ";&|\n":
            finish_word()
            if segments[-1]:
                segments.append([])
        elif char.isspace():
            finish_word()
        else:
            word.append(char)
        index += 1
    if quote is not None:
        return []
    finish_word()
    if any(segment and segment[0] in {
        "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
        "case", "esac", "select", "function", "{", "}",
    } for segment in segments):
        return []
    return [segment for segment in segments if segment]


def segment_invokes_prepare(
    words: list[str],
    description: str | None = None,
    invocation_id: str | None = None,
) -> bool:
    raw_words = words
    try:
        words = [shlex.split(word, posix=True)[0] for word in raw_words]
    except (ValueError, IndexError):
        return False
    assignments: dict[str, str] = {}

    def record_assignment(word: str) -> None:
        name, value = word.split("=", 1)
        assignments[name] = value

    index = 0
    while index < len(words) and SHELL_ASSIGNMENT_RE.fullmatch(raw_words[index]):
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


CONVERSATION_ROW = re.compile(rb'"type"\s*:\s*"(?:user|assistant)"')


def active_prepare_invocations(
    path: Path,
    source_id: str | None,
    description: str,
    invocation_id: str | None = None,
    not_before: float | None = None,
) -> list[PrepareInvocation]:
    data = path.read_bytes()

    def belongs(record: dict[str, Any]) -> bool:
        if source_id is None:
            return record.get("isSidechain") is not True
        return record.get("agentId") == source_id and record.get("isSidechain") is True

    invocations = []
    for offset, record in matching_records(data, invocation_id or "super_subagent.py", path):
        if record.get("type") != "assistant" or not belongs(record):
            continue
        assistant_uuid = record.get("uuid")
        message = record.get("message")
        if not isinstance(assistant_uuid, str) or not assistant_uuid or not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue
        timestamp = record_timestamp(record)
        if not_before is not None and not timestamp >= not_before:
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Bash":
                continue
            tool_id = block.get("id")
            tool_input = block.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if not isinstance(tool_id, str) or not tool_id or not isinstance(command, str):
                continue
            if not command_invokes_prepare(command, description, invocation_id):
                continue
            if any(
                belongs(result) and tool_id in message_tool_result_ids(result)
                for _, result in matching_records(data, tool_id, path)
            ):
                continue
            response_id = message.get("id")
            response_id = response_id if isinstance(response_id, str) and response_id else None
            group = [
                (position, member) for position, member in matching_records(data, response_id or assistant_uuid, path)
                if member.get("type") == "assistant" and belongs(member)
                and isinstance(member.get("message"), dict) and (
                    member["message"].get("id") == response_id
                    if response_id is not None else member.get("uuid") == assistant_uuid
                )
            ]
            if not group:
                raise UsageError(f"could not locate response {response_id or assistant_uuid!r} in {path}")
            group_start, first = group[0]
            group_end = data.find(b"\n", group[-1][0])
            group_end = len(data) if group_end < 0 else group_end + 1
            later = [
                candidate for _, candidate in matching_records(data, CONVERSATION_ROW, path, group_end)
                if belongs(candidate)
            ]
            if any(candidate.get("type") == "assistant"
                   and isinstance(candidate.get("message"), dict)
                   and candidate["message"].get("stop_reason") is not None for candidate in later):
                continue
            if invocation_id is None and any(
                candidate.get("type") == "user" and not message_tool_result_ids(candidate) for candidate in later
            ):
                continue
            boundary_uuid = first.get("parentUuid")
            if boundary_uuid is not None and not isinstance(boundary_uuid, str):
                continue
            invocations.append(PrepareInvocation(
                source_id, path, assistant_uuid, response_id, tool_id, boundary_uuid,
                None if timestamp == float("-inf") else timestamp,
                first, data if source_id is not None else None, group_start,
            ))
    return invocations


def resolved_invocation(invocation: PrepareInvocation) -> CallerSelection:
    snapshot = None
    if invocation.source_id is not None:
        assert invocation.source_bytes is not None
        data = invocation.source_bytes
        offset = invocation.response_offset
        prefix = decode_transcript_records(data[:offset], invocation.transcript)
        tail = decode_transcript_records(data[offset:], invocation.transcript, data.count(b"\n", 0, offset) + 1)
        template = next(
            (record for record in reversed(prefix) if record.get("uuid") == invocation.boundary_uuid),
            invocation.template_record or {},
        )
        snapshot = WorkerSnapshot(
            prefix, invocation.boundary_uuid, template, len(tail),
            sorted({tool_id for record in tail for tool_id in message_tool_use_ids(record)}),
        )
    return CallerSelection(invocation.source_id, invocation.transcript, invocation, snapshot)


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
        return CallerSelection(
            None, parent_jsonl, None,
            main_reference=read_parent_reference(parent_jsonl, args.parent_last_uuid),
        )

    transcript_candidates = agent_transcript_candidates(subagents_directory)
    selected = source_agent_from_options(args, transcript_candidates)
    if selected is not None:
        source_id, source_path = selected
        source_is_explicit = bool(args.from_agent_id or args.from_agent_name)
        invocation_id = caller_invocation_id(required=not source_is_explicit)
        invocations = active_prepare_invocations(
            source_path, source_id, args.description, invocation_id,
            None if source_is_explicit else time.time() - CALLER_INVOCATION_MAX_AGE_SECONDS,
        )
        if not invocations and not source_is_explicit:
            raise UsageError(f"could not identify the active prepare invocation in {source_path}")
        if len(invocations) > 1:
            raise UsageError(f"multiple active prepare invocations in {source_path}")
        invocation = invocations[0] if invocations else None
        if invocation is not None:
            return resolved_invocation(invocation)
        else:
            records = read_transcript_records(source_path)
            last_message = next(
                (record for record in reversed(records)
                 if record.get("type") in {"user", "assistant"}
                 and isinstance(record.get("uuid"), str)
                 and record.get("agentId") == source_id
                 and record.get("isSidechain") is True),
                None,
            )
            if last_message is None:
                raise UsageError(f"source agent {source_id} has no sidechain messages")
            snapshot = WorkerSnapshot(records, last_message["uuid"], last_message, 0, [])
        return CallerSelection(source_id, source_path, invocation, snapshot)

    invocation_id = caller_invocation_id(required=True)
    timeout = float(args.caller_discovery_timeout)
    if not 0 < timeout <= MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS:
        raise UsageError(
            "--caller-discovery-timeout must be greater than zero and at most "
            f"{MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS:g} seconds"
        )
    deadline = time.monotonic() + timeout
    started_wall = time.time()
    read_errors: dict[Path, str] = {}

    def scan_transcript(path: Path, source_id: str | None) -> list[PrepareInvocation]:
        try:
            found = active_prepare_invocations(
                path, source_id, args.description, invocation_id,
                started_wall - CALLER_INVOCATION_MAX_AGE_SECONDS,
            )
        except (OSError, UsageError) as error:
            read_errors[path] = (
                str(error) if isinstance(error, UsageError)
                else f"could not read transcript {path}: {error}"
            )
            return []
        read_errors.pop(path, None)
        return found

    invocations: list[PrepareInvocation] = []
    stable_signature: tuple[tuple[str, str], ...] | None = None
    stable_since: float | None = None
    while True:
        found = scan_transcript(parent_jsonl, None)
        for source_id, transcript in agent_transcript_candidates(subagents_directory):
            found.extend(scan_transcript(transcript, source_id))
        invocations = found
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

    if not invocations and read_errors:
        raise UsageError(
            "could not identify the caller; transcript read failures:\n"
            + "\n".join(read_errors.values())
        )
    if len(invocations) != 1:
        labels = [invocation.source_id or "main" for invocation in invocations]
        rendered = ", ".join(labels) if labels else "none"
        raise UsageError(
            "could not identify one caller from its active prepare invocation "
            f"(candidates: {rendered}); pass --from-main or --from-agent-id"
        )
    invocation = invocations[0]
    if invocation.source_id is None and args.parent_last_uuid is not None:
        return CallerSelection(
            None, parent_jsonl, invocation,
            main_reference=read_parent_reference(parent_jsonl, args.parent_last_uuid),
        )
    return resolved_invocation(invocation)


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


def copy_worker_records(
    records: list[dict[str, Any]], source_id: str, target_id: str,
) -> list[dict[str, Any]]:
    # Agent loading filters by agentId, including fork-context-ref and content
    # replacements. UUIDs are scoped to that file; retain them and every link.
    copied = copy.deepcopy(records)
    for record in copied:
        if record.get("agentId") == source_id:
            record["agentId"] = target_id
    return copied


def make_completed_tail_record(
    source_record: dict[str, Any],
    parent_uuid: str | None,
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
    snapshot: WorkerSnapshot,
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

    copied = copy_worker_records(snapshot.records, source_id, agent_id)
    reference = next(
        (record for record in reversed(copied)
         if record.get("type") == "fork-context-ref" and record.get("agentId") == agent_id),
        None,
    )
    last_message = snapshot.template_record.get("message")
    has_completed_assistant_tail = (
        snapshot.boundary_uuid is not None
        and snapshot.template_record.get("type") == "assistant"
        and isinstance(last_message, dict)
        and last_message.get("stop_reason") is not None
    )
    boundary_records: list[dict[str, Any]] = []
    if not has_completed_assistant_tail:
        boundary_records.append(
            make_completed_tail_record(
                snapshot.template_record, snapshot.boundary_uuid, agent_id, session_id
            )
        )
    target_records = copied + boundary_records
    if reference is None:
        target_records.append(
            make_fork_seed(
                agent_id,
                session_id,
                boundary_records[-1]["uuid"] if boundary_records else snapshot.boundary_uuid,
                snapshot.template_record,
            )
        )
    transcript_bytes = b"".join(json_line(record) for record in target_records)
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
        "copied_sidechain_records": sum(record.get("type") != "fork-context-ref" for record in copied),
        "snapshot_boundary_records": len(boundary_records),
        "dropped_active_records": snapshot.dropped_records,
        "dropped_unmatched_tool_use_ids": snapshot.dropped_tool_ids,
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
        return prepare_main_agent(args, selection)
    if args.parent_last_uuid:
        raise UsageError("--parent-last-uuid is valid only when preparing from main")
    if selection.worker_snapshot is None:
        raise UsageError("caller discovery did not provide the worker's history")
    return prepare_worker_agent(
        args,
        selection.source_id,
        selection.transcript,
        selection.worker_snapshot,
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
        for record in read_transcript_records(parent_jsonl)
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

    records = read_transcript_records(transcript_path)
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
