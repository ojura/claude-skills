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
from typing import Any, Iterable


AGENT_ID_RE = re.compile(r"^a[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
WORKFLOW_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9_-]*$")
DEFAULT_CALLER_DISCOVERY_TIMEOUT_SECONDS = 5.0
MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS = 60.0
CALLER_DISCOVERY_POLL_INTERVAL_SECONDS = 0.025
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
            "agent id must start with 'a' and contain only letters, digits, '-' or '_'"
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
        try:
            write_bytes_exclusive(temporary_meta, json_line(metadata))
            write_bytes_exclusive(temporary_jsonl, transcript_bytes)
            # Publish metadata first. A crash can leave unused metadata, but never a
            # discoverable transcript without its metadata.
            os.rename(temporary_meta, meta_path)
            os.rename(temporary_jsonl, jsonl_path)
            fsync_directory(subagents_directory)
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


def prepare_main_agent(args: argparse.Namespace) -> dict[str, Any]:
    description, agent_name, agent_id = agent_identity(args)
    model, fork_another_model, cache_warning = fork_model_settings(args)

    projects_root = Path(args.projects_root).expanduser().resolve()
    parent_jsonl, session_id = find_parent_jsonl(
        parent_jsonl=args.parent_jsonl,
        session_id=args.session_id,
        projects_root=projects_root,
    )
    parent_uuid, context_length, parent_record = read_parent_reference(
        parent_jsonl, args.parent_last_uuid
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
        "spawnDepth": args.spawn_depth,
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


def command_invokes_prepare(command: str) -> bool:
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(
        word.rsplit("/", 1)[-1] == "super_subagent.py"
        and index + 1 < len(words)
        and words[index + 1] == "prepare"
        for index, word in enumerate(words)
    )


def unresolved_prepare_invocation(path: Path) -> bool:
    try:
        records = read_agent_records(path)
    except (OSError, UsageError):
        return False
    completed = {
        tool_id
        for record in records
        for tool_id in message_tool_result_ids(record)
    }
    for record in records:
        if record.get("type") != "assistant":
            continue
        content = record.get("message", {}).get("content")
        if not isinstance(content, list):
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
                and command_invokes_prepare(command)
            ):
                return True
    return False


def agent_id_from_filename(path: Path) -> str:
    name = path.name
    if not name.startswith("agent-") or not name.endswith(".jsonl"):
        raise UsageError(f"not an agent transcript name: {path}")
    return validate_agent_id(name[len("agent-") : -len(".jsonl")])


def source_agent_from_options(
    args: argparse.Namespace,
    subagents_directory: Path,
) -> tuple[str, Path] | None:
    explicit_id = args.from_agent_id
    environment_id = os.environ.get("CLAUDE_CODE_AGENT_ID") or os.environ.get(
        "CLAUDE_AGENT_ID"
    )
    if explicit_id:
        source_id = validate_agent_id(explicit_id)
    elif args.from_agent_name:
        matches: list[str] = []
        for metadata_path in sorted(subagents_directory.glob("agent-*.meta.json")):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if metadata.get("name") == args.from_agent_name:
                candidate = metadata_path.name[len("agent-") : -len(".meta.json")]
                matches.append(validate_agent_id(candidate))
        if len(matches) != 1:
            rendered = ", ".join(matches) if matches else "none"
            raise UsageError(
                f"--from-agent-name {args.from_agent_name!r} matched {rendered}; "
                "pass --from-agent-id"
            )
        source_id = matches[0]
    elif environment_id:
        source_id = validate_agent_id(environment_id)
    else:
        return None

    source_path = subagents_directory / f"agent-{source_id}.jsonl"
    if not source_path.is_file():
        raise UsageError(f"source agent transcript does not exist: {source_path}")
    return source_id, source_path


def discover_prepare_caller(
    args: argparse.Namespace,
    parent_jsonl: Path,
    subagents_directory: Path,
) -> tuple[str, Path] | None:
    if args.from_main:
        return None

    selected = source_agent_from_options(args, subagents_directory)
    if selected is not None:
        return selected

    timeout = float(args.caller_discovery_timeout)
    if not 0 < timeout <= MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS:
        raise UsageError(
            "--caller-discovery-timeout must be greater than zero and at most "
            f"{MAX_CALLER_DISCOVERY_TIMEOUT_SECONDS:g} seconds"
        )
    deadline = time.monotonic() + timeout
    main_is_caller = False
    agent_candidates: list[tuple[str, Path]] = []
    while True:
        main_is_caller = unresolved_prepare_invocation(parent_jsonl)
        agent_candidates = [
            (agent_id_from_filename(path), path)
            for path in sorted(subagents_directory.glob("agent-*.jsonl"))
            if unresolved_prepare_invocation(path)
        ]
        candidate_count = int(main_is_caller) + len(agent_candidates)
        remaining = deadline - time.monotonic()
        if candidate_count or remaining <= 0:
            break
        # The Bash child can begin before the transcript writer publishes the
        # assistant tool_use that identifies its caller.
        time.sleep(min(CALLER_DISCOVERY_POLL_INTERVAL_SECONDS, remaining))

    labels = (["main"] if main_is_caller else []) + [
        agent_id for agent_id, _ in agent_candidates
    ]
    if len(labels) != 1:
        rendered = ", ".join(labels) if labels else "none"
        raise UsageError(
            "could not identify one caller from its active prepare invocation "
            f"(candidates: {rendered}); pass --from-main or --from-agent-id"
        )
    if main_is_caller:
        return None
    return agent_candidates[0]


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


def active_sidechain_chain(
    records: list[dict[str, Any]], source_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], int, list[str]]:
    references = [
        record
        for record in records
        if record.get("type") == "fork-context-ref"
        and record.get("agentId") == source_id
    ]
    if not references:
        raise UsageError(
            f"source agent {source_id} has no fork-context-ref and is not a fork"
        )
    reference = references[-1]

    sidechain = [
        record
        for record in records
        if record.get("agentId") == source_id
        and record.get("isSidechain") is True
        and isinstance(record.get("uuid"), str)
    ]
    if not sidechain:
        raise UsageError(f"source agent {source_id} has no sidechain messages")

    by_uuid = {str(record["uuid"]): record for record in sidechain}
    parent_uuids = {
        str(record["parentUuid"])
        for record in sidechain
        if isinstance(record.get("parentUuid"), str)
    }
    leaves = [
        record
        for record in sidechain
        if record["uuid"] not in parent_uuids
        and not (
            record.get("type") == "system"
            and record.get("subtype") == "compact_boundary"
        )
    ]
    if not leaves:
        raise UsageError(f"source agent {source_id} has no sidechain leaf")
    leaf_positions = {str(record["uuid"]): index for index, record in enumerate(sidechain)}
    leaf = max(leaves, key=lambda record: leaf_positions[str(record["uuid"])])

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
    chain = recover_parallel_tool_results(sidechain, base_chain)

    tool_results = {
        tool_id
        for record in chain
        for tool_id in message_tool_result_ids(record)
    }
    unresolved = sorted(
        {
            tool_id
            for record in chain
            for tool_id in entry_tool_use_ids(record)
            if tool_id not in tool_results
        }
    )
    cut_index = len(chain)
    if unresolved:
        unresolved_set = set(unresolved)
        for index, record in enumerate(chain):
            if entry_tool_use_ids(record) & unresolved_set:
                response_id = record.get("message", {}).get("id")
                cut_index = index
                if isinstance(response_id, str):
                    while cut_index > 0:
                        previous = chain[cut_index - 1]
                        if previous.get("message", {}).get("id") != response_id:
                            break
                        cut_index -= 1
                break
    elif chain[-1].get("type") == "assistant" and chain[-1].get(
        "message", {}
    ).get("stop_reason") is None:
        response_id = chain[-1].get("message", {}).get("id")
        cut_index = len(chain) - 1
        if isinstance(response_id, str):
            while cut_index > 0:
                previous = chain[cut_index - 1]
                if previous.get("message", {}).get("id") != response_id:
                    break
                cut_index -= 1

    completed = chain[:cut_index]
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
    return reference, completed, len(chain) - cut_index, unresolved


def remap_sidechain(
    records: list[dict[str, Any]], target_id: str
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
        source_records, source_id
    )
    copied = remap_sidechain(completed, agent_id)
    reference = copy.deepcopy(source_reference)
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
    transcript_bytes = json_line(reference) + b"".join(
        json_line(record) for record in target_records
    )
    metadata = {
        "agentType": "general-purpose" if fork_another_model else "fork",
        **({} if fork_another_model else {"isFork": True}),
        "description": description,
        "name": agent_name,
        "spawnDepth": args.spawn_depth,
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
        "parent_last_uuid": reference.get("parentLastUuid"),
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
    selected = discover_prepare_caller(args, parent_jsonl, subagents_directory)
    if selected is None:
        return prepare_main_agent(args)
    if args.parent_last_uuid:
        raise UsageError("--parent-last-uuid is valid only when preparing from main")
    source_id, source_path = selected
    return prepare_worker_agent(args, source_id, source_path)


def workflow_directory(
    workflow: str,
    subagents_directory: Path,
) -> Path:
    supplied = Path(workflow).expanduser()
    if supplied.is_absolute() or "/" in workflow:
        candidate = supplied.resolve()
    else:
        if not WORKFLOW_ID_RE.fullmatch(workflow):
            raise UsageError(
                "workflow must be a wf_* run id or a path to its agent directory"
            )
        candidate = (subagents_directory / "workflows" / workflow).resolve()
    if not candidate.is_dir():
        raise UsageError(f"workflow agent directory does not exist: {candidate}")
    return candidate


def source_agent_ids(source_directory: Path) -> list[str]:
    result = []
    for transcript in sorted(source_directory.glob("agent-*.jsonl")):
        agent_id = transcript.name[len("agent-") : -len(".jsonl")]
        validate_agent_id(agent_id)
        metadata = source_directory / f"agent-{agent_id}.meta.json"
        if not metadata.is_file():
            raise UsageError(f"workflow agent has no metadata file: {metadata}")
        result.append(agent_id)
    if not result:
        raise UsageError(f"workflow has no agent transcripts: {source_directory}")
    return result


def same_symlink(destination: Path, source: Path) -> bool:
    return destination.is_symlink() and destination.resolve() == source.resolve()


def expected_override_metadata(source: Path, model: str) -> bytes:
    try:
        metadata = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UsageError(f"cannot read workflow metadata {source}: {error}") from error
    if not isinstance(metadata, dict):
        raise UsageError(f"workflow metadata is not a JSON object: {source}")
    metadata["model"] = model
    if metadata.get("isFork") is True:
        del metadata["isFork"]
        metadata["agentType"] = "general-purpose"
    return json_line(metadata)


def regular_file_has_bytes(path: Path, expected: bytes) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.read_bytes() == expected
    except OSError:
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
        destination_jsonl = subagents_directory / source_jsonl.name
        destination_meta = subagents_directory / source_meta.name
        override_bytes = (
            expected_override_metadata(source_meta, model_override)
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
        default=1,
        help="metadata spawn depth; defaults to a top-level fork",
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
    if getattr(args, "spawn_depth", 1) < 0:
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
