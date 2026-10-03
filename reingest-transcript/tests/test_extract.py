import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "extract.py"
SPEC = importlib.util.spec_from_file_location("reingest_extract", SCRIPT)
extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(extract)


def turn(uuid, parent, role, text, *, sidechain=False, agent=None, **extra):
    record = {
        "uuid": uuid,
        "parentUuid": parent,
        "type": role,
        "message": {"role": role, "content": text},
        "isSidechain": sidechain,
        **extra,
    }
    if agent is not None:
        record["agentId"] = agent
    return record


class ActivePathTests(unittest.TestCase):
    def test_main_path_excludes_sidechain(self):
        objects = [
            turn("main-1", None, "user", "main root"),
            turn("worker-1", None, "user", "worker root", sidechain=True, agent="a1"),
            turn("main-2", "main-1", "assistant", "main answer"),
            turn("worker-2", "worker-1", "assistant", "worker answer", sidechain=True, agent="a1"),
        ]
        path, breaks = extract.active_path(objects)
        self.assertEqual([record["uuid"] for record in path], ["main-1", "main-2"])
        self.assertEqual(breaks, [])

    def test_sidechain_reports_reference_and_omits_main(self):
        objects = [
            turn("main-1", None, "user", "inherited main text"),
            {
                "type": "fork-context-ref",
                "agentId": "a1",
                "parentSessionId": "session-1",
                "parentLastUuid": "main-1",
                "contextLength": 10,
            },
            turn("worker-1", None, "user", "local brief", sidechain=True, agent="a1"),
            turn("worker-2", "worker-1", "assistant", "local answer", sidechain=True, agent="a1"),
        ]
        path, breaks, agent, reference, reference_count = extract.sidechain_path(objects)
        self.assertEqual(agent, "a1")
        self.assertEqual([record["uuid"] for record in path], ["worker-1", "worker-2"])
        self.assertEqual(breaks, [])
        self.assertEqual(reference["parentLastUuid"], "main-1")
        self.assertEqual(reference_count, 1)

    def test_general_worker_has_local_root_without_reference(self):
        objects = [
            turn("worker-1", None, "user", "brief", sidechain=True, agent="a1"),
            turn("worker-2", "worker-1", "assistant", "answer", sidechain=True, agent="a1"),
        ]
        path, breaks, agent, reference, reference_count = extract.sidechain_path(objects)
        self.assertEqual(agent, "a1")
        self.assertEqual([record["uuid"] for record in path], ["worker-1", "worker-2"])
        self.assertEqual(breaks, [])
        self.assertIsNone(reference)
        self.assertEqual(reference_count, 0)

    def test_sidechain_crosses_worker_compaction(self):
        objects = [
            {
                "type": "fork-context-ref",
                "agentId": "a1",
                "parentSessionId": "session-1",
                "parentLastUuid": "main-tail",
            },
            turn("before-1", None, "user", "before compaction", sidechain=True, agent="a1"),
            turn("before-2", "before-1", "assistant", "old answer", sidechain=True, agent="a1"),
            turn(
                "compact-1",
                None,
                "user",
                "worker summary",
                sidechain=True,
                agent="a1",
                isCompactSummary=True,
                logicalParentUuid="before-2",
            ),
            turn("after-1", "compact-1", "assistant", "after compaction", sidechain=True, agent="a1"),
        ]
        path, breaks, _, _, _ = extract.sidechain_path(objects)
        self.assertEqual(
            [record["uuid"] for record in path],
            ["before-1", "before-2", "compact-1", "after-1"],
        )
        self.assertEqual(breaks, [])

    def test_multiple_sidechain_agents_require_selection(self):
        objects = [
            turn("a1-1", None, "user", "one", sidechain=True, agent="a1"),
            turn("a2-1", None, "user", "two", sidechain=True, agent="a2"),
        ]
        with self.assertRaisesRegex(ValueError, "multiple sidechain agents"):
            extract.sidechain_path(objects)
        path, _, agent, _, _ = extract.sidechain_path(objects, "a2")
        self.assertEqual(agent, "a2")
        self.assertEqual([record["uuid"] for record in path], ["a2-1"])


class SidechainCliTests(unittest.TestCase):
    def test_cli_emits_local_records_and_fork_boundary(self):
        objects = [
            turn("main-1", None, "user", "must not be emitted"),
            {
                "type": "fork-context-ref",
                "agentId": "a1",
                "parentSessionId": "session-1",
                "parentLastUuid": "main-1",
            },
            turn("worker-1", None, "user", "local brief", sidechain=True, agent="a1"),
            turn("worker-2", "worker-1", "assistant", "local answer", sidechain=True, agent="a1"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "agent-a1.jsonl"
            output = Path(directory) / "sidechain.jsonl"
            source.write_text("".join(json.dumps(record) + "\n" for record in objects))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(source),
                    str(output),
                    "--scope",
                    "sidechain",
                    "--mode",
                    "text",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            rendered_text = output.read_text()
            rendered = [json.loads(line) for line in rendered_text.splitlines()]

        self.assertEqual(rendered[0]["scope"], "sidechain")
        self.assertEqual(rendered[0]["agent"], "a1")
        self.assertEqual(rendered[0]["fork"]["parentLastUuid"], "main-1")
        self.assertEqual([record.get("t") for record in rendered[1:]], ["local brief", "local answer"])
        self.assertNotIn("must not be emitted", rendered_text)
        self.assertIn("fork boundary: parent session=session-1  parent tail=main-1", completed.stdout)
        self.assertIn("inherited main history stops at fork-context-ref", completed.stdout)

    def test_cli_reads_both_sides_of_worker_compaction(self):
        objects = [
            {
                "type": "fork-context-ref",
                "agentId": "a1",
                "parentSessionId": "session-1",
                "parentLastUuid": "main-tail",
            },
            turn("before-1", None, "user", "before worker compaction", sidechain=True, agent="a1"),
            turn("before-2", "before-1", "assistant", "old worker answer", sidechain=True, agent="a1"),
            turn(
                "compact-1",
                None,
                "user",
                "worker compaction summary",
                sidechain=True,
                agent="a1",
                isCompactSummary=True,
                logicalParentUuid="before-2",
            ),
            turn("after-1", "compact-1", "assistant", "new worker answer", sidechain=True, agent="a1"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "agent-a1.jsonl"
            output = Path(directory) / "sidechain.jsonl"
            source.write_text("".join(json.dumps(record) + "\n" for record in objects))
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(source),
                    str(output),
                    "--scope",
                    "sidechain",
                    "--mode",
                    "text",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            rendered = [json.loads(line) for line in output.read_text().splitlines()]

        self.assertEqual(
            [record.get("t") for record in rendered[1:]],
            [
                "before worker compaction",
                "old worker answer",
                "worker compaction summary",
                "new worker answer",
            ],
        )
        self.assertIn("what the LATEST compaction dropped", completed.stdout)


if __name__ == "__main__":
    unittest.main()
