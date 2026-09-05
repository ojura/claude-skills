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
            "spawn_depth": 1,
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

    def write_source_agent(self, agent_id, records, name="source-fork"):
        subagents = self.project / self.session_id / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        transcript = subagents / f"agent-{agent_id}.jsonl"
        transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        (subagents / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "fork", "isFork": True, "name": name}) + "\n",
            encoding="utf-8",
        )
        return transcript

    def test_description_slug_uses_readable_words(self):
        self.assertEqual(
            super_subagent.description_slug("Write a skill named /super-subagent, with a helper"),
            "write-skill-super-subagent-helper",
        )
        self.assertEqual(super_subagent.description_slug("!!!"), "agent")

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
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_prepare_main",
                                "name": "Bash",
                                "input": {
                                    "command": (
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

        result = super_subagent.prepare_agent(
            self.prepare_args(
                model="inherit",
                fork_another_model=False,
                from_main=False,
            )
        )
        reference = json.loads(Path(result["jsonl"]).read_text().splitlines()[0])

        self.assertEqual(reference["parentLastUuid"], root_uuid)
        self.assertNotIn("source_agent_id", result)
        self.assertEqual(result["mode"], "fork")

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
                "message": {
                    "id": "response-active",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_active",
                            "name": "Bash",
                            "input": {
                                "command": "python3 super_subagent.py prepare task"
                            },
                        }
                    ],
                    "stop_reason": "tool_use",
                },
            },
        ]

    def test_prepare_from_worker_copies_completed_chain_and_preserves_source(self):
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
        self.assertTrue(source_uuids.isdisjoint(target_uuids))
        copied_tool_references = [
            record["sourceToolAssistantUUID"]
            for record in target_sidechain
            if isinstance(record.get("sourceToolAssistantUUID"), str)
        ]
        self.assertEqual(len(copied_tool_references), 1)
        self.assertTrue(set(copied_tool_references) <= target_uuids)
        self.assertTrue(set(copied_tool_references).isdisjoint(source_uuids))
        rendered = Path(result["jsonl"]).read_text()
        self.assertIn("sidechain-only-orchid-915", rendered)
        self.assertNotIn("call_active", rendered)
        self.assertEqual(metadata["parentAgentId"], source_id)
        self.assertEqual(metadata["agentType"], "fork")
        self.assertIs(metadata["isFork"], True)

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

    def make_workflow_agent(self, run_id, agent_id, metadata=None):
        source = self.project / self.session_id / "subagents" / "workflows" / run_id
        source.mkdir(parents=True, exist_ok=True)
        (source / f"agent-{agent_id}.jsonl").write_text(
            json.dumps({"type": "user", "agentId": agent_id}) + "\n"
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


if __name__ == "__main__":
    unittest.main()
