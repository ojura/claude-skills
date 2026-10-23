import argparse
import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "super_subagent.py"
SPEC = importlib.util.spec_from_file_location("super_subagent", SCRIPT)
super_subagent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(super_subagent)

TEST_CALLER_INVOCATION_ID = "0123456789abcdef0123456789abcdef"
TEST_STALE_INVOCATION_ID = "fedcba9876543210fedcba9876543210"


def invocation_command(command, invocation_id=TEST_CALLER_INVOCATION_ID):
    return f"{super_subagent.CALLER_INVOCATION_ENV}={invocation_id} {command}"


class SuperSubagentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.project = self.projects / "-tmp-project"
        self.project.mkdir(parents=True)
        self.session_id = "11111111-2222-4333-8444-555555555555"
        self.parent = self.project / f"{self.session_id}.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def caller_environment(
        self,
        invocation_id=TEST_CALLER_INVOCATION_ID,
        source_id=None,
    ):
        values = {super_subagent.CALLER_INVOCATION_ENV: invocation_id}
        if source_id is not None:
            values["CLAUDE_CODE_AGENT_ID"] = source_id
        return mock.patch.dict(os.environ, values)

    def write_parent(self, entries):
        self.parent.write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries),
            encoding="utf-8",
        )

    def prepare_args(self, **changes):
        values = {
            "description": "Review the proxy retirement tests",
            "model": "gpt-5.6-sol",
            "fork_another_model": True,
            "name": None,
            "agent_id": "areview-proxy-retirement-tests-0123456789abcdef",
            "spawn_depth": None,
            "parent_last_uuid": None,
            "from_main": True,
            "from_agent_id": None,
            "from_agent_name": None,
            "caller_discovery_timeout": 1.0,
            "session_id": self.session_id,
            "parent_jsonl": None,
            "projects_root": str(self.projects),
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def link_args(self, workflow, **changes):
        values = {
            "workflow": workflow,
            "agent_id": None,
            "model": None,
            "session_id": self.session_id,
            "parent_jsonl": None,
            "projects_root": str(self.projects),
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def worker_prepare_args(self, **changes):
        values = vars(
            self.prepare_args(
                description="Continue the source fork",
                model="inherit",
                fork_another_model=False,
                agent_id="atarget-worker-child-0123456789abcdef",
                from_main=False,
                from_agent_id="asource-fork-0123456789abcdef",
            )
        )
        values.update(changes)
        return argparse.Namespace(**values)

    def write_source_agent(
        self,
        agent_id,
        records,
        name="source-fork",
        metadata=None,
        directory=None,
    ):
        subagents = directory or self.project / self.session_id / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        transcript = subagents / f"agent-{agent_id}.jsonl"
        transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        stored_metadata = metadata or {
            "agentType": "fork",
            "isFork": True,
            "name": name,
        }
        (subagents / f"agent-{agent_id}.meta.json").write_text(
            json.dumps(stored_metadata) + "\n",
            encoding="utf-8",
        )
        return transcript

    def padding_history(self):
        return [
            {"type": "user", "uuid": "before", "parentUuid": None,
             "promptId": "keep-this-prompt", "message": {"role": "user", "content": "keep \u0000 and \t"}},
            {"type": "assistant", "uuid": "after", "parentUuid": "before",
             "message": {"id": "kept-response", "role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "after padding"}]}},
        ]

    def main_prepare_record(self, command):
        return {
            "type": "assistant", "uuid": "prepare-call", "parentUuid": "before",
            "isSidechain": False, "timestamp": time.time(),
            "message": {"id": "prepare-response", "role": "assistant", "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": "call_prepare", "name": "Bash",
                                     "input": {"command": command}}]},
        }

    def test_shared_reader_ignores_only_padding_rows(self):
        records = self.padding_history()
        for padding in (b"\x00" * 1683 + b"\n", b"\n", b" \t\r\n", b"\x00 \t\x00\r\n"):
            with self.subTest(padding_bytes=len(padding)):
                raw = super_subagent.json_line(records[0]) + padding + super_subagent.json_line(records[1])
                self.parent.write_bytes(raw)
                self.assertEqual(super_subagent.read_transcript_records(self.parent), records)
                self.assertEqual(super_subagent.read_parent_reference(self.parent, None), ("after", 2, records[1]))
                self.assertEqual(self.parent.read_bytes(), raw)

    def test_prepare_main_ignores_nul_padding(self):
        records = self.padding_history()
        raw = super_subagent.json_line(records[0]) + b"\x00" * 1683 + b"\n" + super_subagent.json_line(records[1])
        self.parent.write_bytes(raw)
        result = super_subagent.prepare_agent(self.prepare_args())
        reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])
        self.assertEqual(reference["parentLastUuid"], "after")
        self.assertEqual(reference["contextLength"], 2)
        self.assertEqual(self.parent.read_bytes(), raw)

    def test_detects_both_nonce_segments_after_padding_with_python_flags(self):
        descriptions = ["First padded child", "Second padded child"]
        nonces = [TEST_CALLER_INVOCATION_ID, TEST_STALE_INVOCATION_ID]
        command = " && ".join(
            invocation_command(f"python3 -I -B {SCRIPT} prepare '{description}'", nonce)
            for description, nonce in zip(descriptions, nonces)
        )
        raw = (super_subagent.json_line(self.padding_history()[0]) + b"\x00" * 1683 + b"\n"
               + super_subagent.json_line(self.main_prepare_record(command)))
        self.parent.write_bytes(raw)
        for number, (description, nonce) in enumerate(zip(descriptions, nonces)):
            with self.subTest(segment=number):
                with self.caller_environment(nonce):
                    result = super_subagent.prepare_agent(self.prepare_args(
                        description=description, from_main=False, model="inherit", fork_another_model=False,
                        agent_id=f"apadded-main-{number:016x}",
                    ))
                reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])
                self.assertEqual(reference["parentLastUuid"], "before")
                self.assertEqual(reference["contextLength"], 1)
        self.assertEqual(self.parent.read_bytes(), raw)

    def test_worker_copy_preserves_valid_history_around_padding(self):
        self.write_parent([{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}])
        source_id = "asource-fork-0123456789abcdef"
        records = self.completed_parallel_worker_records(source_id)
        source = self.write_source_agent(source_id, records)
        lines = [super_subagent.json_line(record) for record in records]
        raw = b"".join(lines[:2]) + b"\x00" * 1683 + b"\n \t\r\n" + b"".join(lines[2:])
        source.write_bytes(raw)
        with self.caller_environment(source_id=source_id):
            result = super_subagent.prepare_agent(self.worker_prepare_args(from_agent_id=None))
        copied = [json.loads(line) for line in Path(result["jsonl"]).read_text().splitlines()]
        self.assert_copied_history(records[:-2], copied, source_id, result["agent_id"])
        self.assertEqual(source.read_bytes(), raw)

    def test_shared_reader_reports_actual_malformed_json_at_its_physical_row(self):
        records = self.padding_history()
        for malformed in (b'{"broken":}\n', b'not-json\n', b'\x00{"type":"user"}\n', b'{"content":"a\x00b"}\n'):
            with self.subTest(malformed=repr(malformed)):
                raw = (super_subagent.json_line(records[0]) + b"\n" + b"\x00" * 1683 + b"\n"
                       + malformed + super_subagent.json_line(records[1]))
                self.parent.write_bytes(raw)
                for reader in (lambda: super_subagent.read_transcript_records(self.parent),
                               lambda: super_subagent.read_parent_reference(self.parent, None)):
                    with self.assertRaises(super_subagent.UsageError) as raised:
                        reader()
                    self.assertIn(str(self.parent), str(raised.exception))
                    self.assertIn("at line 4:", str(raised.exception))
                    self.assertIn("invalid JSON", str(raised.exception))
                self.assertEqual(self.parent.read_bytes(), raw)

    def test_shared_reader_retains_incomplete_final_write_handling(self):
        records = self.padding_history()
        prefix = super_subagent.json_line(records[0]) + b"\x00" * 1683 + b"\n" + super_subagent.json_line(records[1])
        self.parent.write_bytes(prefix + b'{"type":')
        self.assertEqual(super_subagent.read_transcript_records(self.parent), records)
        self.assertEqual(super_subagent.read_parent_reference(self.parent, None), ("after", 2, records[1]))
        self.parent.write_bytes(prefix + b'{"type":\n')
        for reader in (lambda: super_subagent.read_transcript_records(self.parent),
                       lambda: super_subagent.read_parent_reference(self.parent, None)):
            with self.assertRaisesRegex(super_subagent.UsageError, "at line 4:"):
                reader()
        self.parent.write_bytes(prefix.rstrip(b"\n"))
        self.assertEqual(super_subagent.read_transcript_records(self.parent), records)

    def test_automatic_discovery_reports_read_failure_instead_of_missing_caller(self):
        description = "Unreadable parent"
        command = invocation_command(f"python3 -I -B {SCRIPT} prepare '{description}'")
        raw = (super_subagent.json_line(self.padding_history()[0]) + b'{"broken":}\n'
               + super_subagent.json_line(self.main_prepare_record(command)))
        self.parent.write_bytes(raw)
        with self.caller_environment():
            with self.assertRaises(super_subagent.UsageError) as raised:
                super_subagent.prepare_agent(self.prepare_args(
                    description=description, from_main=False, caller_discovery_timeout=0.02,
                ))
        diagnostic = str(raised.exception)
        self.assertIn("transcript read failures", diagnostic)
        self.assertIn(str(self.parent), diagnostic)
        self.assertIn("at line 2:", diagnostic)
        self.assertEqual(self.parent.read_bytes(), raw)

    def test_automatic_discovery_reports_io_failure_with_the_path(self):
        self.write_parent(self.padding_history())
        error = PermissionError(13, "Permission denied", str(self.parent))
        with self.caller_environment():
            with mock.patch.object(super_subagent, "read_transcript_records", side_effect=error):
                with self.assertRaises(super_subagent.UsageError) as raised:
                    super_subagent.prepare_agent(self.prepare_args(
                        from_main=False, caller_discovery_timeout=0.02,
                    ))
        diagnostic = str(raised.exception)
        self.assertIn("transcript read failures", diagnostic)
        self.assertIn(str(self.parent), diagnostic)
        self.assertIn("Permission denied", diagnostic)

    def test_unreadable_other_worker_does_not_hide_a_valid_caller(self):
        description = "Readable main"
        command = invocation_command(f"python3 -I -B {SCRIPT} prepare '{description}'")
        self.write_parent([self.padding_history()[0], self.main_prepare_record(command)])
        other = self.write_source_agent("aunreadable-worker-0123456789abcdef", [])
        other.write_bytes(b'{"broken":}\n')
        with self.caller_environment():
            result = super_subagent.prepare_agent(self.prepare_args(description=description, from_main=False))
        reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])
        self.assertEqual(reference["parentLastUuid"], "before")

    def test_description_slug_uses_readable_words(self):
        self.assertEqual(
            super_subagent.description_slug("Write a skill named /super-subagent, with a helper"),
            "write-skill-super-subagent-helper",
        )
        self.assertEqual(super_subagent.description_slug("!!!"), "agent")

    def test_agent_id_validation_matches_send_message_recipient_syntax(self):
        valid = (
            "a0123456789abcdef",
            "areview-worker-0123456789abcdef",
            f"a{'x' * 63}-0123456789abcdef",
        )
        invalid = (
            "a0123456789abcde",
            "areview-worker-0123456789abcde",
            "areview-worker-0123456789ABCDEf",
            f"a{'x' * 64}-0123456789abcdef",
            "areview.worker-0123456789abcdef",
        )
        for agent_id in valid:
            with self.subTest(agent_id=agent_id):
                self.assertEqual(super_subagent.validate_agent_id(agent_id), agent_id)
        for agent_id in invalid:
            with self.subTest(agent_id=agent_id):
                with self.assertRaises(super_subagent.UsageError):
                    super_subagent.validate_agent_id(agent_id)

    def test_prepare_writes_fork_reference_and_arbitrary_model(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.write_parent(
            [
                {
                    "type": "user",
                    "uuid": root_uuid,
                    "parentUuid": None,
                    "isSidechain": False,
                    "message": {"role": "user", "content": "task"},
                }
            ]
        )

        result = super_subagent.prepare_agent(self.prepare_args())
        transcript = Path(result["jsonl"])
        metadata_path = Path(result["meta"])
        records = [json.loads(line) for line in transcript.read_text().splitlines()]
        reference, seed = records
        metadata = json.loads(metadata_path.read_text())

        self.assertEqual(reference["type"], "fork-context-ref")
        self.assertEqual(reference["agentId"], result["agent_id"])
        self.assertEqual(reference["parentSessionId"], self.session_id)
        self.assertEqual(reference["parentLastUuid"], root_uuid)
        self.assertEqual(seed["agentId"], result["agent_id"])
        self.assertIs(seed["isSidechain"], True)
        self.assertEqual(seed["type"], "user")
        self.assertEqual(
            seed["message"]["content"][0]["text"],
            super_subagent.FORK_BOILERPLATE
            + "\n\nYour directive is the following user message.",
        )
        self.assertEqual(metadata["agentType"], "general-purpose")
        self.assertNotIn("isFork", metadata)
        self.assertEqual(metadata["name"], "review-proxy-retirement-tests")
        self.assertEqual(metadata["model"], "gpt-5.6-sol")
        self.assertEqual(result["mode"], "fork-another-model")
        self.assertIn("cannot reuse the parent's prompt cache", result["cache_warning"])
        self.assertEqual(stat.S_IMODE(transcript.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(metadata_path.stat().st_mode), 0o600)

    def test_prepare_detects_main_caller(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        active_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.write_parent(
            [
                {"type": "user", "uuid": root_uuid, "parentUuid": None},
                {
                    "type": "assistant",
                    "uuid": active_uuid,
                    "parentUuid": root_uuid,
                    "timestamp": time.time(),
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_prepare_main",
                                "name": "Bash",
                                "input": {
                                    "command": invocation_command(
                                        "python3 /home/juraj/claude-skills/"
                                        "super-subagent/scripts/super_subagent.py "
                                        "prepare 'Main child'"
                                    )
                                },
                            }
                        ],
                    },
                },
            ]
        )

        with self.caller_environment():
            result = super_subagent.prepare_agent(
                self.prepare_args(
                    description="Main child",
                    model="inherit",
                    fork_another_model=False,
                    from_main=False,
                )
            )
        reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])

        self.assertEqual(reference["parentLastUuid"], root_uuid)
        self.assertNotIn("source_agent_id", result)
        self.assertEqual(result["mode"], "fork")

    def test_automatic_caller_detection_requires_internal_invocation_id(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root", "parentUuid": None}]
        )
        with mock.patch.dict(os.environ):
            os.environ.pop(super_subagent.CALLER_INVOCATION_ENV, None)
            with self.assertRaisesRegex(
                super_subagent.UsageError, "requires the invocation id"
            ):
                super_subagent.prepare_agent(
                    self.prepare_args(
                        description="Main child",
                        model="inherit",
                        fork_another_model=False,
                        from_main=False,
                    )
                )

    def test_prepare_requires_acknowledgement_for_another_model(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.write_parent(
            [{"type": "user", "uuid": root_uuid, "parentUuid": None}]
        )

        with self.assertRaisesRegex(
            super_subagent.UsageError, "requires --fork-another-model"
        ):
            super_subagent.prepare_agent(
                self.prepare_args(fork_another_model=False)
            )

        subagents = self.project / self.session_id / "subagents"
        self.assertFalse(
            (subagents / "agent-areview-proxy-retirement-tests-0123456789abcdef.jsonl").exists()
        )

    def test_prepare_cli_warns_about_separate_model_cache(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.write_parent(
            [{"type": "user", "uuid": root_uuid, "parentUuid": None}]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = super_subagent.main(
                [
                    "prepare",
                    "Review the cache behavior",
                    "--model",
                    "gpt-5.6-sol",
                    "--fork-another-model",
                    "--from-main",
                    "--agent-id",
                    "acache-warning-0123456789abcdef",
                    "--parent-jsonl",
                    str(self.parent),
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn("cannot reuse the parent's prompt cache", stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["mode"], "fork-another-model")

    def test_prepare_inherit_uses_builtin_fork_metadata(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.write_parent(
            [{"type": "user", "uuid": root_uuid, "parentUuid": None}]
        )

        result = super_subagent.prepare_agent(
            self.prepare_args(model="inherit", fork_another_model=False)
        )
        metadata = json.loads(Path(result["meta"]).read_text())

        self.assertEqual(metadata["agentType"], "fork")
        self.assertIs(metadata["isFork"], True)
        self.assertEqual(metadata["model"], "inherit")
        self.assertEqual(metadata["spawnDepth"], 1)
        self.assertEqual(result["mode"], "fork")
        self.assertNotIn("cache_warning", result)

    def test_prepare_rejects_another_model_flag_with_inherit(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.write_parent(
            [{"type": "user", "uuid": root_uuid, "parentUuid": None}]
        )

        with self.assertRaisesRegex(
            super_subagent.UsageError, "requires a non-inherit"
        ):
            super_subagent.prepare_agent(
                self.prepare_args(model="inherit", fork_another_model=True)
            )

    def test_prepare_avoids_unmatched_parent_tool_use(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        tool_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.write_parent(
            [
                {
                    "type": "user",
                    "uuid": root_uuid,
                    "parentUuid": None,
                    "message": {"role": "user", "content": "task"},
                },
                {
                    "type": "assistant",
                    "uuid": tool_uuid,
                    "parentUuid": root_uuid,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "call_active", "name": "Bash", "input": {}}
                        ],
                    },
                },
            ]
        )

        result = super_subagent.prepare_agent(self.prepare_args())
        reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])
        self.assertEqual(reference["parentLastUuid"], root_uuid)

    def test_prepare_rejects_explicit_unfinished_parent_tool_use(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        tool_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.write_parent(
            [
                {
                    "type": "user",
                    "uuid": root_uuid,
                    "parentUuid": None,
                    "message": {"role": "user", "content": "task"},
                },
                {
                    "type": "assistant",
                    "uuid": tool_uuid,
                    "parentUuid": root_uuid,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_explicit_active",
                                "name": "Bash",
                                "input": {},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "parentUuid": tool_uuid,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_explicit_active",
                                "content": "later result",
                            }
                        ],
                    },
                },
            ]
        )

        with self.assertRaisesRegex(
            super_subagent.UsageError, "ends at an unfinished tool call"
        ):
            super_subagent.prepare_agent(
                self.prepare_args(parent_last_uuid=tool_uuid)
            )

    def test_prepare_refuses_to_replace_existing_agent_file(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.write_parent(
            [{"type": "user", "uuid": root_uuid, "parentUuid": None}]
        )
        subagents = self.project / self.session_id / "subagents"
        subagents.mkdir(parents=True)
        existing = subagents / "agent-areview-proxy-retirement-tests-0123456789abcdef.jsonl"
        existing.write_text("keep me\n")

        with self.assertRaisesRegex(super_subagent.UsageError, "already exists"):
            super_subagent.prepare_agent(self.prepare_args())
        self.assertEqual(existing.read_text(), "keep me\n")
        self.assertFalse(
            (subagents / "agent-areview-proxy-retirement-tests-0123456789abcdef.meta.json").exists()
        )

    def source_fork_records(self, source_id):
        return [
            {
                "type": "fork-context-ref",
                "agentId": source_id,
                "parentSessionId": self.session_id,
                "parentLastUuid": "parent-root-uuid",
                "contextLength": 12,
            },
            {
                "type": "user",
                "uuid": "source-user-1",
                "parentUuid": None,
                "isSidechain": True,
                "agentId": source_id,
                "message": {"role": "user", "content": "sidechain-only-orchid-915"},
            },
            {
                "type": "assistant",
                "uuid": "source-assistant-tool-1",
                "parentUuid": "source-user-1",
                "isSidechain": True,
                "agentId": source_id,
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "id": "response-completed-tools",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_completed_1",
                            "name": "Read",
                            "input": {"file_path": "/tmp/example-1"},
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
            {
                "type": "assistant",
                "uuid": "source-assistant-tool-2",
                "parentUuid": "source-assistant-tool-1",
                "isSidechain": True,
                "agentId": source_id,
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "id": "response-completed-tools",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_completed_2",
                            "name": "Read",
                            "input": {"file_path": "/tmp/example-2"},
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
            {
                "type": "user",
                "uuid": "source-tool-result-1",
                "parentUuid": "source-assistant-tool-1",
                "isSidechain": True,
                "agentId": source_id,
                "timestamp": "2026-01-01T00:00:03Z",
                "sourceToolAssistantUUID": "source-assistant-tool-1",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_completed_1",
                            "content": "result 1",
                        }
                    ],
                },
            },
            {
                "type": "user",
                "uuid": "source-tool-result-2",
                "parentUuid": "source-assistant-tool-2",
                "isSidechain": True,
                "agentId": source_id,
                "timestamp": "2026-01-01T00:00:04Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_completed_2",
                            "content": "result 2",
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "uuid": "source-active-thinking",
                "parentUuid": "source-tool-result-2",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "response-active",
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "active"}],
                    "stop_reason": None,
                },
            },
            {
                "type": "assistant",
                "uuid": "source-active-tool",
                "parentUuid": "source-active-thinking",
                "isSidechain": True,
                "agentId": source_id,
                "timestamp": time.time(),
                "message": {
                    "id": "response-active",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_active",
                            "name": "Bash",
                            "input": {
                                "command": invocation_command(
                                    "python3 super_subagent.py prepare "
                                    "'Continue the source fork'"
                                )
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
        ]

    def completed_parallel_worker_records(self, source_id):
        records = self.source_fork_records(source_id)
        prefix = records[:2]
        prefix.append({
            "type": "assistant", "uuid": "parallel-prefix",
            "parentUuid": "source-user-1", "isSidechain": True,
            "agentId": source_id, "apiBlockIndex": 0,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "id": "completed-parallel", "role": "assistant",
                "content": [{"type": "thinking", "thinking": "fixture", "signature": "keep"}],
                "stop_reason": None,
            },
        })
        calls, results = {}, {}
        for number, parent in enumerate(
            ("parallel-prefix", "parallel-call-1", "parallel-result-1", "parallel-result-2"), 1
        ):
            calls[number] = {
                "type": "assistant", "uuid": f"parallel-call-{number}",
                "parentUuid": parent, "isSidechain": True, "agentId": source_id,
                "apiBlockIndex": number, "timestamp": float(number),
                "message": {
                    "id": "completed-parallel", "role": "assistant",
                    "content": [{"type": "tool_use", "id": f"call_{number}",
                                 "name": "Read", "input": {"file_path": f"/fixture/{number}"}}],
                    "stop_reason": "tool_use",
                },
            }
            results[number] = {
                "type": "user", "uuid": f"parallel-result-{number}",
                "parentUuid": f"parallel-call-{number}", "isSidechain": True,
                "agentId": source_id, "timestamp": float(number) + 0.5,
                "sourceToolAssistantUUID": f"parallel-call-{number}",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": f"call_{number}",
                    "content": f"result {number}",
                }]},
            }
        prefix.extend((calls[1], calls[2], results[1], calls[3], results[2],
                       calls[4], results[3], results[4]))
        prefix.append({
            "type": "assistant", "uuid": "after-parallel", "parentUuid": "parallel-result-4",
            "isSidechain": True, "agentId": source_id, "apiBlockIndex": 0,
            "message": {"id": "another-response", "role": "assistant",
                        "content": [{"type": "text", "text": "after parallel calls"}],
                        "stop_reason": "end_turn"},
        })
        records[-2]["parentUuid"] = "after-parallel"
        return prefix + records[-2:]

    def assert_copied_history(self, source_records, copied_records, source_id, target_id):
        expected = json.loads(json.dumps(source_records))
        for record in expected:
            if record.get("agentId") == source_id:
                record["agentId"] = target_id
        self.assertEqual(copied_records[:len(expected)], expected)

    def prepare_history_fixture(self, records, **changes):
        self.write_parent([{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}])
        source_id = "asource-fork-0123456789abcdef"
        source = self.write_source_agent(source_id, records)
        before = source.read_bytes()
        result = super_subagent.prepare_agent(self.worker_prepare_args(**changes))
        copied = [json.loads(line) for line in Path(result["jsonl"]).read_text().splitlines()]
        self.assertEqual(source.read_bytes(), before)
        return result, copied

    def test_prepare_worker_preserves_branched_completed_history(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.completed_parallel_worker_records(source_id)
        result, copied = self.prepare_history_fixture(records)
        expected = records[:-2]
        self.assertEqual(
            [record.get("message") for record in copied[:len(expected)]],
            [record.get("message") for record in expected],
        )
        self.assert_copied_history(expected, copied, source_id, result["agent_id"])
        if len(copied) > len(expected):
            self.assertEqual(copied[len(expected)]["parentUuid"], "after-parallel")

        second = super_subagent.prepare_agent(self.worker_prepare_args(
            from_agent_id=result["agent_id"], agent_id="athird-worker-0123456789abcdef"
        ))
        recopied = [json.loads(line) for line in Path(second["jsonl"]).read_text().splitlines()]
        self.assert_copied_history(copied, recopied, result["agent_id"], second["agent_id"])

    def test_prepare_worker_preserves_existing_noncanonical_history(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.completed_parallel_worker_records(source_id)
        by_uuid = {record.get("uuid"): record for record in records}
        records[2:-3] = [by_uuid[name] for name in (
            "parallel-prefix", "parallel-call-1", "parallel-call-2",
            "parallel-result-2", "parallel-call-4", "parallel-result-1",
            "parallel-call-3", "parallel-result-3", "parallel-result-4",
        )]
        result, copied = self.prepare_history_fixture(records)
        self.assert_copied_history(records[:-2], copied, source_id, result["agent_id"])

    def test_prepare_worker_preserves_block_metadata_without_interpreting_it(self):
        source_id = "asource-fork-0123456789abcdef"
        for number, index in enumerate((None, True, -1, 1.5, "3", 2)):
            with self.subTest(index=index):
                records = self.completed_parallel_worker_records(source_id)
                record = next(record for record in records if record.get("uuid") == "parallel-call-3")
                record["apiBlockIndex"] = index
                result, copied = self.prepare_history_fixture(
                    records, agent_id=f"aindices-{number:016x}"
                )
                self.assert_copied_history(records[:-2], copied, source_id, result["agent_id"])

    def test_prepare_worker_preserves_nonmessage_records_and_identifiers(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.completed_parallel_worker_records(source_id)
        records[1]["promptId"] = "original-prompt-id"
        records[1]["message"]["content"] = f"The source is {source_id}"
        records.insert(2, {
            "type": "content-replacement", "agentId": source_id,
            "messageId": "source-user-1", "replacement": "fixture replacement",
        })
        records.insert(3, {"type": "atis-latch", "atis": "fixture"})
        records.insert(-2, {
            "type": "api-request-blob", "agentId": source_id,
            "hash": "fixture-hash", "blob": {"agentId": source_id},
        })
        result, copied = self.prepare_history_fixture(records)
        self.assert_copied_history(records[:-2], copied, source_id, result["agent_id"])
        if len(copied) > len(records) - 2:
            self.assertEqual(copied[len(records) - 2]["parentUuid"], "after-parallel")

    def test_prepare_worker_preserves_repeated_records(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.completed_parallel_worker_records(source_id)
        records.insert(-2, json.loads(json.dumps(records[1])))
        result, copied = self.prepare_history_fixture(records)
        self.assert_copied_history(records[:-2], copied, source_id, result["agent_id"])

    def test_prepare_worker_explicit_source_keeps_an_unfinished_nonhelper_exchange(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.completed_parallel_worker_records(source_id)
        # An explicit source without a matched prepare invocation is copied
        # through EOF. The helper does not repair its pending tool calls.
        current = records[-1]
        current["message"]["content"] = [
            {"type": "tool_use", "id": "terminal-one", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "terminal-two", "name": "Read", "input": {}},
        ]
        records.append({
            "type": "user", "uuid": "terminal-result", "parentUuid": current["uuid"],
            "agentId": source_id, "isSidechain": True, "timestamp": time.time() + 1,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "terminal-one", "content": "one"}
            ]},
        })
        result, copied = self.prepare_history_fixture(records)
        self.assert_copied_history(records, copied, source_id, result["agent_id"])
        self.assertEqual(result["dropped_unmatched_tool_use_ids"], [])
        self.assertEqual(result["dropped_active_records"], 0)

    def test_prepare_worker_copies_a_completed_file_with_older_unfinished_turns(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.source_fork_records(source_id)
        records.append({
            "type": "user", "uuid": "new-user-turn", "parentUuid": records[-1]["uuid"],
            "isSidechain": True, "agentId": source_id,
            "timestamp": time.time() + 1,
            "message": {"role": "user", "content": "A later instruction"},
        })
        result, copied = self.prepare_history_fixture(records)
        self.assert_copied_history(records, copied, source_id, result["agent_id"])
        self.assertEqual(result["dropped_active_records"], 0)
        self.assertEqual(copied[-1]["parentUuid"], "new-user-turn")

    def test_prepare_worker_explicit_source_preserves_every_record_through_eof(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.source_fork_records(source_id)[:2]
        records.extend([
            {"type": "assistant", "uuid": "completed", "parentUuid": "source-user-1",
             "agentId": source_id, "isSidechain": True, "timestamp": "2026-01-01T00:00:03Z",
             "message": {"id": "completed", "role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "completed"}]}},
            {"type": "assistant", "uuid": "older-branch", "parentUuid": "source-user-1",
             "agentId": source_id, "isSidechain": True, "timestamp": "2026-01-01T00:00:02Z",
             "message": {"id": "older-branch", "role": "assistant", "stop_reason": None,
                         "content": [{"type": "text", "text": "unfinished"}]}},
        ])
        result, copied = self.prepare_history_fixture(records)
        self.assert_copied_history(records, copied, source_id, result["agent_id"])
        if len(copied) > len(records):
            self.assertEqual(copied[len(records)]["parentUuid"], "older-branch")
        self.assertEqual(result["dropped_active_records"], 0)

    def test_prepare_worker_keeps_the_active_invocation_boundary_after_new_input(self):
        source_id = "asource-fork-0123456789abcdef"
        records = self.source_fork_records(source_id)
        records.append({
            "type": "user", "uuid": "input-after-helper", "parentUuid": records[-1]["uuid"],
            "agentId": source_id, "isSidechain": True, "timestamp": time.time() + 1,
            "message": {"role": "user", "content": "Input arriving after the helper started"},
        })
        with self.caller_environment():
            result, copied = self.prepare_history_fixture(records, from_agent_id=None)
        self.assert_copied_history(records[:-3], copied, source_id, result["agent_id"])
        self.assertEqual(result["dropped_active_records"], 3)

    def test_worker_copy_uses_the_history_already_resolved_by_discovery(self):
        self.write_parent([{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}])
        source_id = "asource-fork-0123456789abcdef"
        records = self.completed_parallel_worker_records(source_id)
        source = self.write_source_agent(source_id, records)
        discover = super_subagent.discover_prepare_caller
        read = super_subagent.read_transcript_records
        resolved = False

        def read_before_discovery_only(*args):
            self.assertFalse(resolved, "the copier reread a resolved source")
            return read(*args)

        def discover_then_append(*args):
            nonlocal resolved
            selection = discover(*args)
            resolved = True
            with source.open("a") as handle:
                handle.write(json.dumps({
                    "type": "user", "uuid": "arrived-after-discovery",
                    "agentId": source_id, "isSidechain": True,
                    "parentUuid": records[-1]["uuid"],
                    "message": {"role": "user", "content": "later input"},
                }) + "\n")
            return selection

        with mock.patch.object(super_subagent, "read_transcript_records", side_effect=read_before_discovery_only):
            with mock.patch.object(super_subagent, "discover_prepare_caller", side_effect=discover_then_append):
                result = super_subagent.prepare_agent(self.worker_prepare_args())
        copied = [json.loads(line) for line in Path(result["jsonl"]).read_text().splitlines()]
        self.assert_copied_history(records[:-2], copied, source_id, result["agent_id"])
        self.assertNotIn("arrived-after-discovery", Path(result["jsonl"]).read_text())

    def test_prepare_from_worker_copies_history_and_preserves_source(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "asource-fork-0123456789abcdef"
        source = self.write_source_agent(
            source_id, self.source_fork_records(source_id)
        )
        source_bytes = source.read_bytes()

        result = super_subagent.prepare_agent(self.worker_prepare_args())
        target_records = [
            json.loads(line) for line in Path(result["jsonl"]).read_text().splitlines()
        ]
        target_sidechain = target_records[1:]
        metadata = json.loads(Path(result["meta"]).read_text())

        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertEqual(target_records[0]["type"], "fork-context-ref")
        self.assertEqual(target_records[0]["agentId"], result["agent_id"])
        self.assertEqual(len(target_sidechain), 6)
        self.assertEqual(result["copied_sidechain_records"], 5)
        self.assertEqual(result["snapshot_boundary_records"], 1)
        self.assertEqual(result["dropped_active_records"], 2)
        self.assertEqual(result["dropped_unmatched_tool_use_ids"], ["call_active"])
        self.assertEqual(target_sidechain[0]["parentUuid"], None)
        prior_uuids = {target_sidechain[0]["uuid"]}
        for current in target_sidechain[1:]:
            self.assertIn(current["parentUuid"], prior_uuids)
            prior_uuids.add(current["uuid"])
        self.assertEqual(target_sidechain[-1]["type"], "assistant")
        self.assertEqual(
            target_sidechain[-1]["message"]["stop_reason"], "end_turn"
        )
        self.assertIn(
            "next user message is the new directive",
            target_sidechain[-1]["message"]["content"][0]["text"],
        )
        source_uuids = {
            record["uuid"]
            for record in self.source_fork_records(source_id)
            if "uuid" in record
        }
        target_uuids = {record["uuid"] for record in target_sidechain}
        copied_history = target_records[:1 + result["copied_sidechain_records"]]
        self.assert_copied_history(
            self.source_fork_records(source_id)[:-2], copied_history, source_id, result["agent_id"]
        )
        self.assertNotIn(target_sidechain[-1]["uuid"], source_uuids)
        copied_tool_references = [
            record["sourceToolAssistantUUID"]
            for record in target_sidechain
            if isinstance(record.get("sourceToolAssistantUUID"), str)
        ]
        self.assertEqual(len(copied_tool_references), 1)
        self.assertTrue(set(copied_tool_references) <= target_uuids)
        self.assertEqual(copied_tool_references, ["source-assistant-tool-1"])
        rendered = Path(result["jsonl"]).read_text()
        self.assertIn("sidechain-only-orchid-915", rendered)
        self.assertNotIn("call_active", rendered)
        self.assertEqual(metadata["parentAgentId"], source_id)
        self.assertEqual(metadata["agentType"], "fork")
        self.assertIs(metadata["isFork"], True)
        self.assertEqual(metadata["spawnDepth"], 2)

    def test_prepare_from_worker_rejects_main_uuid_boundary(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "asource-fork-0123456789abcdef"
        self.write_source_agent(source_id, self.source_fork_records(source_id))

        with self.assertRaisesRegex(
            super_subagent.UsageError, "valid only when preparing from main"
        ):
            super_subagent.prepare_agent(
                self.worker_prepare_args(parent_last_uuid="parent-root-uuid")
            )

    def test_prepare_from_worker_discovers_active_helper_invocation(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "asource-fork-0123456789abcdef"
        self.write_source_agent(source_id, self.source_fork_records(source_id))

        with self.caller_environment(source_id=source_id):
            result = super_subagent.prepare_agent(
                self.worker_prepare_args(from_agent_id=None)
            )

        self.assertEqual(result["source_agent_id"], source_id)
        self.assertEqual(result["dropped_unmatched_tool_use_ids"], ["call_active"])

    def test_prepare_from_worker_waits_for_delayed_caller_publication(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "asource-fork-0123456789abcdef"

        def publish_source():
            time.sleep(0.1)
            self.write_source_agent(source_id, self.source_fork_records(source_id))

        publisher = threading.Thread(target=publish_source)
        publisher.start()
        try:
            with self.caller_environment():
                result = super_subagent.prepare_agent(
                    self.worker_prepare_args(
                        from_agent_id=None,
                        caller_discovery_timeout=1.0,
                    )
                )
        finally:
            publisher.join()

        self.assertEqual(result["source_agent_id"], source_id)
        self.assertEqual(result["dropped_unmatched_tool_use_ids"], ["call_active"])

    def test_prepare_ignores_stale_unresolved_call_while_current_call_is_published(self):
        description = "Continue the current caller"
        self.write_parent(
            [
                {"type": "user", "uuid": "parent-root-uuid", "parentUuid": None},
                {
                    "type": "assistant",
                    "uuid": "stale-prepare",
                    "parentUuid": "parent-root-uuid",
                    "timestamp": time.time() - 1.0,
                    "message": {
                        "id": "stale-response",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_stale_prepare",
                                "name": "Bash",
                                "input": {
                                    "command": invocation_command(
                                        "python3 super_subagent.py prepare "
                                        "'Continue the current caller'",
                                        TEST_STALE_INVOCATION_ID,
                                    )
                                },
                            }
                        ],
                        "stop_reason": "tool_use",
                    },
                },
            ]
        )
        top = self.project / self.session_id / "subagents"
        top.mkdir(parents=True)
        (top / "agent-abad-id-0123456789abcde.jsonl").write_text("{}\n")

        source_id = "acurrent-caller-0123456789abcdef"
        records = self.source_fork_records(source_id)
        records[-1]["message"]["content"][0]["input"]["command"] = invocation_command(
            "python3 super_subagent.py prepare 'Continue the current caller'"
        )

        def publish_current_source():
            time.sleep(0.6)
            records[-1]["timestamp"] = time.time()
            self.write_source_agent(source_id, records)

        publisher = threading.Thread(target=publish_current_source)
        started = time.monotonic()
        publisher.start()
        try:
            with self.caller_environment():
                result = super_subagent.prepare_agent(
                    self.worker_prepare_args(
                        description=description,
                        from_agent_id=None,
                        caller_discovery_timeout=2.0,
                    )
                )
        finally:
            publisher.join()
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.55)
        self.assertEqual(result["source_agent_id"], source_id)
        self.assertEqual(result["dropped_unmatched_tool_use_ids"], ["call_active"])

    def test_command_detection_requires_an_executed_prepare_command(self):
        description = "Review the caller"
        self.assertFalse(
            super_subagent.command_invokes_prepare(
                "printf '%s' super_subagent.py prepare", description
            )
        )
        self.assertFalse(
            super_subagent.command_invokes_prepare(
                "rg super_subagent.py prepare .", description
            )
        )
        self.assertFalse(
            super_subagent.command_invokes_prepare(
                "python3 other.py super_subagent.py prepare", description
            )
        )
        self.assertTrue(
            super_subagent.command_invokes_prepare(
                "python3 /tmp/super_subagent.py prepare 'Review the caller'",
                description,
            )
        )
        self.assertTrue(
            super_subagent.command_invokes_prepare(
                "printf done && env X=1 python3 /tmp/super_subagent.py "
                "prepare 'Review the caller'",
                description,
            )
        )
        matching = invocation_command(
            "python3 /tmp/super_subagent.py prepare 'Review the caller'"
        )
        stale = invocation_command(
            "python3 /tmp/super_subagent.py prepare 'Review the caller'",
            TEST_STALE_INVOCATION_ID,
        )
        self.assertTrue(
            super_subagent.command_invokes_prepare(
                matching, description, TEST_CALLER_INVOCATION_ID
            )
        )
        self.assertFalse(
            super_subagent.command_invokes_prepare(
                stale, description, TEST_CALLER_INVOCATION_ID
            )
        )

    def test_prepare_main_uses_detected_invocation_boundary_after_append(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        active_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        branch_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        self.write_parent(
            [
                {"type": "user", "uuid": root_uuid, "parentUuid": None},
                {
                    "type": "assistant",
                    "uuid": active_uuid,
                    "parentUuid": root_uuid,
                    "message": {
                        "id": "active-response",
                        "role": "assistant",
                        "content": [],
                        "stop_reason": "tool_use",
                    },
                },
            ]
        )
        invocation = super_subagent.PrepareInvocation(
            None,
            self.parent,
            active_uuid,
            "active-response",
            "call_prepare",
            root_uuid,
            None,
        )

        def discover_then_append(*_args):
            with self.parent.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": branch_uuid,
                            "parentUuid": root_uuid,
                            "message": {
                                "id": "other-response",
                                "role": "assistant",
                                "content": [{"type": "text", "text": "other branch"}],
                                "stop_reason": "end_turn",
                            },
                        }
                    )
                    + "\n"
                )
            return super_subagent.CallerSelection(None, self.parent, invocation)

        with mock.patch.object(
            super_subagent,
            "discover_prepare_caller",
            side_effect=discover_then_append,
        ):
            result = super_subagent.prepare_agent(
                self.prepare_args(
                    description="Main child",
                    model="inherit",
                    fork_another_model=False,
                    from_main=False,
                )
            )
        reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])
        self.assertEqual(reference["parentLastUuid"], root_uuid)
        self.assertNotEqual(reference["parentLastUuid"], branch_uuid)

    def test_prepare_main_handles_parallel_pending_tools(self):
        root_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        first_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        prepare_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        description = "Parallel main child"
        self.write_parent(
            [
                {"type": "user", "uuid": root_uuid, "parentUuid": None},
                {
                    "type": "assistant",
                    "uuid": first_uuid,
                    "parentUuid": root_uuid,
                    "message": {
                        "id": "parallel-response",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_other",
                                "name": "Read",
                                "input": {},
                            }
                        ],
                        "stop_reason": "tool_use",
                    },
                },
                {
                    "type": "assistant",
                    "uuid": prepare_uuid,
                    "parentUuid": first_uuid,
                    "timestamp": time.time(),
                    "message": {
                        "id": "parallel-response",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_prepare",
                                "name": "Bash",
                                "input": {
                                    "command": invocation_command(
                                        "python3 super_subagent.py prepare "
                                        "'Parallel main child'"
                                    )
                                },
                            }
                        ],
                        "stop_reason": "tool_use",
                    },
                },
            ]
        )
        with self.caller_environment():
            result = super_subagent.prepare_agent(
                self.prepare_args(
                    description=description,
                    model="inherit",
                    fork_another_model=False,
                    from_main=False,
                )
            )
        reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])
        self.assertEqual(reference["parentLastUuid"], root_uuid)

    def test_prepare_worker_keeps_turns_after_old_interrupted_call(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "asource-fork-0123456789abcdef"
        description = "Continue after interruption"
        records = [
            {
                "type": "fork-context-ref",
                "agentId": source_id,
                "parentSessionId": self.session_id,
                "parentLastUuid": "parent-root-uuid",
            },
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "isSidechain": True,
                "agentId": source_id,
                "message": {"role": "user", "content": "before"},
            },
            {
                "type": "assistant",
                "uuid": "old-tool",
                "parentUuid": "u1",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "old-response",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_old",
                            "name": "Bash",
                            "input": {
                                "command": invocation_command(
                                    "python3 super_subagent.py prepare "
                                    "'Continue after interruption'"
                                )
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
            {
                "type": "user",
                "uuid": "u2",
                "parentUuid": "old-tool",
                "isSidechain": True,
                "agentId": source_id,
                "message": {"role": "user", "content": "later completed turn"},
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "parentUuid": "u2",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "completed-response",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "completed"}],
                    "stop_reason": "end_turn",
                },
            },
            {
                "type": "assistant",
                "uuid": "current-tool",
                "parentUuid": "a2",
                "isSidechain": True,
                "agentId": source_id,
                "timestamp": time.time(),
                "message": {
                    "id": "current-response",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_current",
                            "name": "Bash",
                            "input": {
                                "command": invocation_command(
                                    "python3 super_subagent.py prepare "
                                    "'Continue after interruption'"
                                )
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
        ]
        self.write_source_agent(source_id, records)
        with self.caller_environment():
            result = super_subagent.prepare_agent(
                self.worker_prepare_args(
                    description=description,
                    from_agent_id=None,
                )
            )
        copied = [
            json.loads(line)
            for line in Path(result["jsonl"]).read_text().splitlines()[1:]
        ]
        rendered = json.dumps(copied)
        self.assertIn("later completed turn", rendered)
        self.assertIn("call_old", rendered)
        self.assertNotIn("call_current", rendered)
        later = next(record for record in copied if record.get("message", {}).get("content") == "later completed turn")
        self.assertEqual(later["parentUuid"], "old-tool")
        self.assert_copied_history(records[1:-1], copied, source_id, result["agent_id"])
        self.assertEqual(result["dropped_unmatched_tool_use_ids"], ["call_current"])

    def test_prepare_worker_preserves_progress_alias_ancestry(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "asource-fork-0123456789abcdef"
        records = self.source_fork_records(source_id)[:2]
        records.extend(
            [
                {
                    "type": "assistant",
                    "uuid": "tool-assistant",
                    "parentUuid": "source-user-1",
                    "isSidechain": True,
                    "agentId": source_id,
                    "message": {
                        "id": "tool-response",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_done",
                                "name": "Read",
                                "input": {},
                            }
                        ],
                        "stop_reason": "tool_use",
                    },
                },
                {
                    "type": "progress",
                    "uuid": "progress-alias",
                    "parentUuid": "tool-assistant",
                },
                {
                    "type": "user",
                    "uuid": "tool-result",
                    "parentUuid": "progress-alias",
                    "isSidechain": True,
                    "agentId": source_id,
                    "sourceToolAssistantUUID": "tool-assistant",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_done",
                                "content": "done",
                            }
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "completed",
                    "parentUuid": "tool-result",
                    "isSidechain": True,
                    "agentId": source_id,
                    "message": {
                        "id": "completed-response",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "complete"}],
                        "stop_reason": "end_turn",
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "current",
                    "parentUuid": "completed",
                    "isSidechain": True,
                    "agentId": source_id,
                    "message": {
                        "id": "current-response",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_active",
                                "name": "Bash",
                                "input": {
                                    "command": (
                                        "python3 super_subagent.py prepare "
                                        "'Continue the source fork'"
                                    )
                                },
                            }
                        ],
                        "stop_reason": "tool_use",
                    },
                },
            ]
        )
        self.write_source_agent(source_id, records)
        result = super_subagent.prepare_agent(self.worker_prepare_args())
        copied = [
            json.loads(line)
            for line in Path(result["jsonl"]).read_text().splitlines()[1:]
        ]
        progress = next(record for record in copied if record.get("type") == "progress")
        tool_result = next(record for record in copied if record.get("uuid") != progress["uuid"] and record.get("type") == "user" and super_subagent.message_tool_result_ids(record))
        self.assertNotIn("agentId", progress)
        self.assertNotIn("isSidechain", progress)
        self.assert_copied_history(records[1:-1], copied, source_id, result["agent_id"])
        self.assertEqual(tool_result["parentUuid"], progress["uuid"])

    def test_prepare_worker_preserves_compact_metadata_uuids(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "asource-fork-0123456789abcdef"
        records = [
            {
                "type": "fork-context-ref",
                "agentId": source_id,
                "parentSessionId": self.session_id,
                "parentLastUuid": "parent-root-uuid",
            },
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "isSidechain": True,
                "agentId": source_id,
                "message": {"role": "user", "content": "before compact"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "a1-response",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "before"}],
                    "stop_reason": "end_turn",
                },
            },
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "compact",
                "parentUuid": None,
                "logicalParentUuid": "a1",
                "isSidechain": True,
                "agentId": source_id,
                "compactMetadata": {
                    "preservedSegment": {
                        "headUuid": "u1",
                        "anchorUuid": "a1",
                        "tailUuid": "a1",
                    },
                    "preservedMessages": {
                        "anchorUuid": "a1",
                        "uuids": ["u1", "a1"],
                        "allUuids": ["u1", "a1"],
                    },
                },
            },
            {
                "type": "user",
                "uuid": "u2",
                "parentUuid": "compact",
                "isSidechain": True,
                "agentId": source_id,
                "message": {"role": "user", "content": "after compact"},
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "parentUuid": "u2",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "a2-response",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "complete"}],
                    "stop_reason": "end_turn",
                },
            },
            {
                "type": "assistant",
                "uuid": "current",
                "parentUuid": "a2",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "current-response",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_active",
                            "name": "Bash",
                            "input": {
                                "command": invocation_command(
                                    "python3 super_subagent.py prepare "
                                    "'Continue the source fork'"
                                )
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
        ]
        self.write_source_agent(source_id, records)
        result = super_subagent.prepare_agent(self.worker_prepare_args())
        copied = [
            json.loads(line)
            for line in Path(result["jsonl"]).read_text().splitlines()[1:]
        ]
        target_uuids = {record["uuid"] for record in copied}
        source_uuids = {record["uuid"] for record in records if "uuid" in record}
        compact = next(record for record in copied if record.get("subtype") == "compact_boundary")["compactMetadata"]
        nested = [
            compact["preservedSegment"][key]
            for key in ("headUuid", "anchorUuid", "tailUuid")
        ] + [
            compact["preservedMessages"]["anchorUuid"],
            *compact["preservedMessages"]["uuids"],
            *compact["preservedMessages"]["allUuids"],
        ]
        self.assertTrue(set(nested) <= target_uuids)
        self.assertTrue(set(nested) <= source_uuids)
        self.assert_copied_history(records[1:-1], copied, source_id, result["agent_id"])

    def test_prepare_general_worker_without_fork_reference(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "ageneral-worker-0123456789abcdef"
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "isSidechain": True,
                "agentId": source_id,
                "message": {"role": "user", "content": "general-only-fact"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "complete",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                },
            },
            {
                "type": "assistant",
                "uuid": "current",
                "parentUuid": "a1",
                "isSidechain": True,
                "agentId": source_id,
                "message": {
                    "id": "current-response",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_active",
                            "name": "Bash",
                            "input": {
                                "command": invocation_command(
                                    "python3 super_subagent.py prepare "
                                    "'Continue the source fork'"
                                )
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
        ]
        self.write_source_agent(
            source_id,
            records,
            metadata={"agentType": "general-purpose", "spawnDepth": 2},
        )
        result = super_subagent.prepare_agent(
            self.worker_prepare_args(from_agent_id=source_id)
        )
        copied = [json.loads(line) for line in Path(result["jsonl"]).read_text().splitlines()]
        self.assertNotEqual(copied[0].get("type"), "fork-context-ref")
        self.assertIn("general-only-fact", json.dumps(copied))
        self.assertIn(
            super_subagent.FORK_BOILERPLATE,
            copied[-1]["message"]["content"][0]["text"],
        )
        metadata = json.loads(Path(result["meta"]).read_text())
        self.assertEqual(metadata["spawnDepth"], 3)

    def test_prepare_finds_worker_inside_workflow_directory(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        source_id = "aworkflow-worker-0123456789abcdef"
        workflow = (
            self.project
            / self.session_id
            / "subagents"
            / "workflows"
            / "wf_worker_test"
        )
        self.write_source_agent(
            source_id,
            self.source_fork_records(source_id),
            metadata={"agentType": "fork", "isFork": True, "spawnDepth": 2},
            directory=workflow,
        )
        with self.caller_environment():
            result = super_subagent.prepare_agent(
                self.worker_prepare_args(
                    from_agent_id=None,
                    description="Continue the source fork",
                )
            )
        self.assertEqual(result["source_agent_id"], source_id)
        metadata = json.loads(Path(result["meta"]).read_text())
        self.assertEqual(metadata["spawnDepth"], 3)

        explicit = super_subagent.prepare_agent(
            self.worker_prepare_args(
                from_agent_id=source_id,
                agent_id="aexplicit-workflow-child-0123456789abcdef",
                spawn_depth=7,
            )
        )
        explicit_metadata = json.loads(Path(explicit["meta"]).read_text())
        self.assertEqual(explicit["source_agent_id"], source_id)
        self.assertEqual(explicit_metadata["spawnDepth"], 7)

    def make_workflow_agent(self, run_id, agent_id, metadata=None, records=None):
        source = self.project / self.session_id / "subagents" / "workflows" / run_id
        source.mkdir(parents=True, exist_ok=True)
        stored_records = records if records is not None else [
            {
                "type": "user",
                "uuid": f"uuid-{agent_id}",
                "parentUuid": None,
                "isSidechain": True,
                "agentId": agent_id,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "resume me"}],
                },
            },
            {
                "type": "assistant",
                "uuid": f"assistant-{agent_id}",
                "parentUuid": f"uuid-{agent_id}",
                "isSidechain": True,
                "agentId": agent_id,
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "id": f"response-{agent_id}",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ready"}],
                    "stop_reason": "end_turn",
                },
            },
        ]
        (source / f"agent-{agent_id}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in stored_records)
        )
        (source / f"agent-{agent_id}.meta.json").write_text(
            json.dumps(metadata or {"agentType": "workflow-subagent"}) + "\n"
        )
        return source

    def test_link_workflow_creates_relative_links_and_is_idempotent(self):
        self.write_parent(
            [{"type": "user", "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}]
        )
        run_id = "wf_01234567-abc"
        source = self.make_workflow_agent(run_id, "a1111111111111111")

        first = super_subagent.link_workflow_agents(self.link_args(run_id))
        second = super_subagent.link_workflow_agents(self.link_args(run_id))
        linked = first["agents"][0]
        destination_jsonl = Path(linked["jsonl"])
        destination_meta = Path(linked["meta"])

        self.assertEqual(first, second)
        self.assertTrue(destination_jsonl.is_symlink())
        self.assertTrue(destination_meta.is_symlink())
        self.assertFalse(os.readlink(destination_jsonl).startswith("/"))
        self.assertEqual(
            destination_jsonl.resolve(),
            (source / "agent-a1111111111111111.jsonl").resolve(),
        )

    def test_link_workflow_model_override_preserves_source_metadata(self):
        self.write_parent(
            [{"type": "user", "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}]
        )
        run_id = "wf_01234567-abc"
        source_metadata = {
            "agentType": "fork",
            "isFork": True,
            "model": "inherit",
            "customField": "keep",
        }
        source = self.make_workflow_agent(
            run_id, "a1111111111111111", source_metadata
        )

        result = super_subagent.link_workflow_agents(
            self.link_args(run_id, model="gpt-5.6-terra")
        )
        destination_meta = Path(result["agents"][0]["meta"])
        resumed_metadata = json.loads(destination_meta.read_text())

        self.assertFalse(destination_meta.is_symlink())
        self.assertEqual(resumed_metadata["model"], "gpt-5.6-terra")
        self.assertNotIn("isFork", resumed_metadata)
        self.assertEqual(resumed_metadata["agentType"], "general-purpose")
        self.assertEqual(resumed_metadata["customField"], "keep")
        self.assertEqual(
            json.loads(
                (source / "agent-a1111111111111111.meta.json").read_text()
            ),
            source_metadata,
        )
        self.assertEqual(stat.S_IMODE(destination_meta.stat().st_mode), 0o600)

        destination_meta.write_text(json.dumps(resumed_metadata, sort_keys=True) + "\n")
        repeated = super_subagent.link_workflow_agents(
            self.link_args(run_id, model="gpt-5.6-terra")
        )
        self.assertEqual(repeated["agents"][0]["agent_id"], "a1111111111111111")

    def test_link_workflow_preflights_every_destination(self):
        self.write_parent(
            [{"type": "user", "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}]
        )
        run_id = "wf_01234567-abc"
        self.make_workflow_agent(run_id, "a1111111111111111")
        self.make_workflow_agent(run_id, "a2222222222222222")
        top = self.project / self.session_id / "subagents"
        conflict = top / "agent-a2222222222222222.meta.json"
        conflict.write_text("different\n")

        with self.assertRaisesRegex(super_subagent.UsageError, "destination already differs"):
            super_subagent.link_workflow_agents(self.link_args(run_id))

        self.assertFalse((top / "agent-a1111111111111111.jsonl").exists())
        self.assertFalse((top / "agent-a1111111111111111.meta.json").exists())
        self.assertEqual(conflict.read_text(), "different\n")

    def test_link_workflow_rolls_back_created_destinations_on_publish_failure(self):
        self.write_parent(
            [{"type": "user", "uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}]
        )
        run_id = "wf_01234567-abc"
        source = self.make_workflow_agent(run_id, "a1111111111111111")
        self.make_workflow_agent(run_id, "a2222222222222222")
        self.make_workflow_agent(run_id, "a3333333333333333")
        top = self.project / self.session_id / "subagents"

        existing_jsonl = top / "agent-a1111111111111111.jsonl"
        existing_meta = top / "agent-a1111111111111111.meta.json"
        existing_jsonl.symlink_to(
            os.path.relpath(
                source / "agent-a1111111111111111.jsonl", existing_jsonl.parent
            )
        )
        existing_meta.symlink_to(
            os.path.relpath(
                source / "agent-a1111111111111111.meta.json", existing_meta.parent
            )
        )

        original_symlink_to = Path.symlink_to

        def fail_last_jsonl(destination, target, target_is_directory=False):
            if destination.name == "agent-a3333333333333333.jsonl":
                raise OSError("injected publication failure")
            return original_symlink_to(destination, target, target_is_directory)

        with mock.patch.object(Path, "symlink_to", new=fail_last_jsonl):
            with self.assertRaisesRegex(OSError, "injected publication failure"):
                super_subagent.link_workflow_agents(self.link_args(run_id))

        self.assertTrue(existing_jsonl.is_symlink())
        self.assertTrue(existing_meta.is_symlink())
        self.assertEqual(
            existing_jsonl.resolve(),
            (source / "agent-a1111111111111111.jsonl").resolve(),
        )
        self.assertEqual(
            existing_meta.resolve(),
            (source / "agent-a1111111111111111.meta.json").resolve(),
        )
        for agent_id in ("a2222222222222222", "a3333333333333333"):
            self.assertFalse((top / f"agent-{agent_id}.jsonl").exists())
            self.assertFalse((top / f"agent-{agent_id}.jsonl").is_symlink())
            self.assertFalse((top / f"agent-{agent_id}.meta.json").exists())
            self.assertFalse((top / f"agent-{agent_id}.meta.json").is_symlink())

    def test_link_workflow_rejects_foreign_session_directory(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        other_session = "99999999-8888-4777-8666-555555555555"
        foreign = (
            self.project
            / other_session
            / "subagents"
            / "workflows"
            / "wf_foreign"
        )
        foreign.mkdir(parents=True)
        agent_id = "aforeign-workflow-0123456789abcdef"
        self.write_source_agent(
            agent_id,
            [
                {
                    "type": "user",
                    "uuid": "foreign-user",
                    "parentUuid": None,
                    "isSidechain": True,
                    "agentId": agent_id,
                }
            ],
            directory=foreign,
        )
        with self.assertRaisesRegex(
            super_subagent.UsageError, "selected parent session"
        ):
            super_subagent.link_workflow_agents(self.link_args(str(foreign)))

    def test_link_workflow_rejects_foreign_fork_reference(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        agent_id = "aforeign-reference-0123456789abcdef"
        records = [
            {
                "type": "fork-context-ref",
                "agentId": agent_id,
                "parentSessionId": "another-session",
                "parentLastUuid": "parent-root-uuid",
            },
            {
                "type": "user",
                "uuid": "workflow-user",
                "parentUuid": None,
                "isSidechain": True,
                "agentId": agent_id,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "resume me"}],
                },
            },
        ]
        self.make_workflow_agent("wf_foreign_ref", agent_id, records=records)
        with self.assertRaisesRegex(
            super_subagent.UsageError, "another parent session"
        ):
            super_subagent.link_workflow_agents(self.link_args("wf_foreign_ref"))

    def test_link_workflow_override_converts_fork_type_without_isfork(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        agent_id = "afork-without-flag-0123456789abcdef"
        source_metadata = {
            "agentType": "fork",
            "model": "inherit",
            "parentAgentId": "aparent-workflow-0123456789abcdef",
        }
        source = self.make_workflow_agent(
            "wf_fork_without_flag", agent_id, metadata=source_metadata
        )
        result = super_subagent.link_workflow_agents(
            self.link_args("wf_fork_without_flag", model="gpt-5.6-sol")
        )
        resumed = json.loads(Path(result["agents"][0]["meta"]).read_text())
        self.assertEqual(resumed["agentType"], "general-purpose")
        self.assertEqual(resumed["model"], "gpt-5.6-sol")
        self.assertNotIn("isFork", resumed)
        self.assertEqual(
            json.loads((source / f"agent-{agent_id}.meta.json").read_text()),
            source_metadata,
        )

    def test_link_workflow_rejects_unresumable_sources(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        cases = (
            ("wf_empty", "aempty-workflow-0123456789abcdef", [], {"agentType": "fork"}),
            (
                "wf_wrong_agent",
                "awrong-agent-0123456789abcdef",
                [
                    {
                        "type": "user",
                        "uuid": "wrong-user",
                        "parentUuid": None,
                        "isSidechain": True,
                        "agentId": "a-different-agent",
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                ],
                {"agentType": "fork"},
            ),
            (
                "wf_no_timestamp",
                "ano-timestamp-0123456789abcdef",
                [
                    {
                        "type": "user",
                        "uuid": "untimestamped-user",
                        "parentUuid": None,
                        "isSidechain": True,
                        "agentId": "ano-timestamp-0123456789abcdef",
                    }
                ],
                {"agentType": "fork"},
            ),
        )
        for run_id, agent_id, records, metadata in cases:
            with self.subTest(run_id=run_id):
                self.make_workflow_agent(run_id, agent_id, metadata, records)
                with self.assertRaisesRegex(
                    super_subagent.UsageError, "no exact-ID sidechain record"
                ):
                    super_subagent.link_workflow_agents(self.link_args(run_id))

        user_only_id = "auser-only-workflow-0123456789abcdef"
        self.make_workflow_agent(
            "wf_user_only",
            user_only_id,
            {"agentType": "fork"},
            [
                {
                    "type": "user",
                    "uuid": "user-only",
                    "parentUuid": None,
                    "isSidechain": True,
                    "agentId": user_only_id,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "enough to resume"}],
                    },
                }
            ],
        )
        linked_user_only = super_subagent.link_workflow_agents(
            self.link_args("wf_user_only")
        )
        self.assertTrue(Path(linked_user_only["agents"][0]["jsonl"]).is_symlink())

        missing_parent_id = "amissing-parent-0123456789abcdef"
        self.make_workflow_agent(
            "wf_missing_parent",
            missing_parent_id,
            {"agentType": "fork"},
            [
                {
                    "type": "user",
                    "uuid": "latest-user",
                    "parentUuid": "absent-parent",
                    "isSidechain": True,
                    "agentId": missing_parent_id,
                    "timestamp": "2026-01-01T00:00:00Z",
                }
            ],
        )
        with self.assertRaisesRegex(
            super_subagent.UsageError, "missing parent UUID"
        ):
            super_subagent.link_workflow_agents(
                self.link_args("wf_missing_parent")
            )

        malformed_id = "amalformed-meta-0123456789abcdef"
        malformed = self.make_workflow_agent("wf_malformed", malformed_id)
        (malformed / f"agent-{malformed_id}.meta.json").write_text("not-json\n")
        with self.assertRaisesRegex(
            super_subagent.UsageError, "cannot read workflow metadata"
        ):
            super_subagent.link_workflow_agents(self.link_args("wf_malformed"))

        non_object_id = "anon-object-meta-0123456789abcdef"
        non_object = self.make_workflow_agent("wf_non_object", non_object_id)
        (non_object / f"agent-{non_object_id}.meta.json").write_text("[]\n")
        with self.assertRaisesRegex(
            super_subagent.UsageError, "metadata is not a JSON object"
        ):
            super_subagent.link_workflow_agents(self.link_args("wf_non_object"))

    def test_publish_agent_files_never_replaces_racing_destination(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        agent_id = "aracing-destination-0123456789abcdef"
        args = self.prepare_args(
            agent_id=agent_id,
            model="inherit",
            fork_another_model=False,
        )
        subagents = self.project / self.session_id / "subagents"
        competing_meta = subagents / f"agent-{agent_id}.meta.json"
        original_link = os.link
        created = False

        def create_competitor_before_link(source, destination, *link_args, **link_kwargs):
            nonlocal created
            destination = Path(destination)
            if destination == competing_meta and not created:
                competing_meta.write_text("competitor\n")
                created = True
            return original_link(source, destination, *link_args, **link_kwargs)

        with mock.patch.object(
            super_subagent.os,
            "link",
            side_effect=create_competitor_before_link,
        ):
            with self.assertRaisesRegex(
                super_subagent.UsageError, "metadata already exists"
            ):
                super_subagent.prepare_agent(args)
        self.assertEqual(competing_meta.read_text(), "competitor\n")
        self.assertFalse((subagents / f"agent-{agent_id}.jsonl").exists())

    def test_publish_agent_files_rolls_back_metadata_after_transcript_failure(self):
        self.write_parent(
            [{"type": "user", "uuid": "parent-root-uuid", "parentUuid": None}]
        )
        agent_id = "atranscript-failure-0123456789abcdef"
        args = self.prepare_args(
            agent_id=agent_id,
            model="inherit",
            fork_another_model=False,
        )
        subagents = self.project / self.session_id / "subagents"
        original_link = os.link

        def fail_transcript_link(source, destination, *link_args, **link_kwargs):
            if str(destination).endswith(".jsonl"):
                raise OSError("injected transcript publication failure")
            return original_link(source, destination, *link_args, **link_kwargs)

        with mock.patch.object(
            super_subagent.os,
            "link",
            side_effect=fail_transcript_link,
        ):
            with self.assertRaisesRegex(OSError, "injected transcript"):
                super_subagent.prepare_agent(args)
        self.assertFalse((subagents / f"agent-{agent_id}.meta.json").exists())
        self.assertFalse((subagents / f"agent-{agent_id}.jsonl").exists())

        result = super_subagent.prepare_agent(args)
        self.assertTrue(Path(result["meta"]).is_file())
        self.assertTrue(Path(result["jsonl"]).is_file())


if __name__ == "__main__":
    unittest.main()
