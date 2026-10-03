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


def boundary(uuid, lpu, uuids, anchor):
    """A compaction boundary whose metadata lists its preserved window, as the harness writes
    it: a null parentUuid and the window's tail as its logicalParentUuid."""
    return {
        "uuid": uuid,
        "parentUuid": None,
        "logicalParentUuid": lpu,
        "type": "system",
        "subtype": "compact_boundary",
        "isSidechain": False,
        "compactMetadata": {
            "preservedSegment": {"headUuid": uuids[0], "anchorUuid": anchor, "tailUuid": uuids[-1]},
            "preservedMessages": {"anchorUuid": anchor, "uuids": uuids, "allUuids": uuids},
        },
    }


def attachment(uuid, parent):
    return {"uuid": uuid, "parentUuid": parent, "type": "attachment", "isSidechain": False}


def run_main(objects):
    """Run the CLI on a main-scope transcript; returns (rendered records, stdout)."""
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "session.jsonl"
        output = Path(directory) / "turns.jsonl"
        source.write_text("".join(json.dumps(record) + "\n" for record in objects))
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), str(source), str(output), "--mode", "text"],
            check=True, text=True, capture_output=True)
        rendered = [json.loads(line) for line in output.read_text().splitlines()]
    return rendered, completed.stdout


def texts(rendered):
    return [record["t"] for record in rendered[1:] if record["r"] != "break"]


def breaks(rendered):
    return [record["t"] for record in rendered[1:] if record["r"] == "break"]


class PreservedTailCycleTests(unittest.TestCase):
    """A compaction can name the tail of its own preserved window as the boundary's
    logicalParentUuid while that tail sits under the new summary, so the chain returns to
    the boundary. The walk must cross it the way ~/claude-patches step P repairs it."""

    def test_tail_under_summary_with_head_naming_predecessor(self):
        # The shape in this session's file (10-01, 10-03 twice): the head keeps its recorded
        # parent, and only the tail, an attachment written after the boundary, hangs under
        # the summary.
        objects = [
            turn("u1", None, "user", "first prompt"),
            turn("a1", "u1", "assistant", "first answer"),
            turn("u2", "a1", "user", "second prompt"),
            turn("w1", "u2", "assistant", "preserved head"),
            turn("w2", "w1", "user", "preserved middle"),
            boundary("B0000001", "t", ["w1", "w2", "t"], "S"),
            attachment("x1", "B0000001"),
            turn("S", "x1", "user", "the compaction summary", isCompactSummary=True),
            attachment("t", "S"),
            turn("u3", "t", "user", "after compaction"),
            turn("a3", "u3", "assistant", "latest answer"),
        ]
        rendered, stdout = run_main(objects)
        self.assertEqual(texts(rendered), [
            "first prompt", "first answer", "second prompt", "the compaction summary",
            "preserved head", "preserved middle", "after compaction", "latest answer"])
        [note] = breaks(rendered)
        self.assertIn("COMPACTION CYCLE REPAIRED", note)
        self.assertIn("recorded", note)
        self.assertIn("REPAIRED: 1 compaction cycle", stdout)
        self.assertNotIn("COVERAGE", stdout)
        self.assertIn("already in your live context", stdout)

    def test_window_under_summary_proven_by_one_origin(self):
        # The shape ~/claude-patches measured: the whole window hangs under the summary.
        objects = [
            turn("u1", None, "user", "first prompt"),
            turn("a1", "u1", "assistant", "pre-compaction reply"),
            boundary("B0000002", "tl", ["hd", "tl"], "S2"),
            turn("S2", "B0000002", "user", "summary two", isCompactSummary=True),
            turn("hd", "S2", "assistant", "preserved head"),
            turn("tl", "hd", "user", "preserved tail"),
            turn("lf", "tl", "assistant", "latest"),
        ]
        rendered, stdout = run_main(objects)
        self.assertEqual(texts(rendered), [
            "first prompt", "pre-compaction reply", "summary two", "preserved head",
            "preserved tail", "latest"])
        [note] = breaks(rendered)
        self.assertIn("proven", note)
        self.assertIn("joined to a1, proven", stdout)
        self.assertNotIn("COVERAGE", stdout)

    def test_positional_join_without_proof_is_reported(self):
        objects = [
            turn("u0", None, "user", "another root"),
            turn("u1", None, "user", "first prompt"),
            turn("a1", "u1", "assistant", "pre-compaction reply"),
            boundary("B0000003", "tl", ["hd", "tl"], "S3"),
            turn("S3", "B0000003", "user", "summary three", isCompactSummary=True),
            turn("hd", "S3", "assistant", "preserved head"),
            turn("tl", "hd", "user", "preserved tail"),
            turn("lf", "tl", "assistant", "latest"),
        ]
        rendered, stdout = run_main(objects)
        self.assertIn("UNPROVEN", breaks(rendered)[0])
        self.assertIn("COVERAGE", stdout)
        self.assertIn("UNPROVEN seam", stdout)
        self.assertIn("line 1 is NOT the start", stdout)

    def test_sidechain_record_before_boundary_is_not_the_predecessor(self):
        objects = [
            turn("u1", None, "user", "first prompt"),
            turn("a1", "u1", "assistant", "pre-compaction reply"),
            turn("sc", None, "user", "a worker's brief", sidechain=True, agent="w1"),
            boundary("B0000004", "tl", ["hd", "tl"], "S4"),
            turn("S4", "B0000004", "user", "summary four", isCompactSummary=True),
            turn("hd", "S4", "assistant", "preserved head"),
            turn("tl", "hd", "user", "preserved tail"),
            turn("lf", "tl", "assistant", "latest"),
        ]
        path, found = extract.active_path([dict(o) for o in objects])
        marker = next(o for o in path if str(o.get("uuid")).startswith("pfgk-"))
        self.assertEqual(marker["parentUuid"], "a1")
        self.assertNotIn("sc", [o.get("uuid") for o in path])
        rendered, stdout = run_main(objects)
        self.assertEqual(texts(rendered), [
            "first prompt", "pre-compaction reply", "summary four", "preserved head",
            "preserved tail", "latest"])
        self.assertIn("joined to a1, proven", stdout)

    def test_any_other_cycle_stops_the_walk_loudly(self):
        objects = [
            turn("p", "q", "user", "p text"),
            turn("q", "p", "assistant", "q text"),
            turn("r", "q", "user", "leaf"),
        ]
        rendered, stdout = run_main(objects)
        self.assertEqual(texts(rendered), ["p text", "q text", "leaf"])
        self.assertIn("CHAIN CYCLE", breaks(rendered)[0])
        self.assertIn("STOPPED", stdout)
        self.assertIn("already on the walked path", stdout)

    def test_parent_never_written_is_bridged(self):
        objects = [
            turn("u1", None, "user", "one"),
            turn("a1", "u1", "assistant", "two"),
            turn("u2", "GONE", "user", "three"),
            turn("a2", "u2", "assistant", "four"),
        ]
        rendered, stdout = run_main(objects)
        self.assertEqual(texts(rendered), ["one", "two", "three", "four"])
        self.assertIn("CHAIN BREAK, BRIDGED", breaks(rendered)[0])
        self.assertIn("bridged at", stdout)


if __name__ == "__main__":
    unittest.main()
