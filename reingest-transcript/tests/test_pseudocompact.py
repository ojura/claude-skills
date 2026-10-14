import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "pseudocompact.py"
SESSION = "00000000-pseudocompact-test-session"


def turn(uuid, parent, role, text, **extra):
    return {"parentUuid": parent, "isSidechain": False, "type": role,
            "message": {"role": role, "content": text}, "uuid": uuid,
            "sessionId": SESSION, **extra}


def system(uuid, parent, subtype, **extra):
    return {"parentUuid": parent, "isSidechain": False, "type": "system",
            "subtype": subtype, "uuid": uuid, "sessionId": SESSION, **extra}


def attachment(uuid, parent, kind, **extra):
    return {"parentUuid": parent, "isSidechain": False, "type": "attachment",
            "attachment": {"type": kind}, "uuid": uuid, "sessionId": SESSION, **extra}


def last_prompt(leaf, text="go on"):
    return {"type": "last-prompt", "lastPrompt": text, "leafUuid": leaf, "sessionId": SESSION}


class PseudocompactTests(unittest.TestCase):
    def run_tool(self, records, *args):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            proc = subprocess.run([sys.executable, str(SCRIPT), str(path), *args],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            written = [json.loads(line) for line in path.read_text().splitlines()]
        return proc.stdout, written

    def boundary(self, written):
        entry, pointer = written[-2], written[-1]
        self.assertTrue(entry["isCompactSummary"])
        self.assertIsNone(entry["parentUuid"])
        self.assertEqual(pointer, {**pointer, "type": "last-prompt", "leafUuid": entry["uuid"]})
        return entry

    def test_leaf_follows_records_written_after_the_last_turn(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "u1", "assistant", "hi"),
            system("t1", "a1", "turn_duration"),
            system("c1", "t1", "local_command", content="<command-name>/model</command-name>"),
            system("c2", "c1", "local_command", content="<local-command-stdout>Kept model</local-command-stdout>"),
            last_prompt("c2"),
        ]
        out, written = self.run_tool(records)
        self.assertNotIn("note:", out)
        self.assertIn("logical parent:  c2 (system/local_command, line 5)", out)
        self.assertIn("3 entries further along the chain from a1 (assistant, line 2), "
                      "the last user or assistant entry in the file", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "c2")

    def test_unanswered_queued_message_stays_an_ancestor(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "u1", "assistant", "hi"),
            attachment("q1", "a1", "queued_command"),
        ]
        out, written = self.run_tool(records)
        self.assertIn("logical parent:  q1 (attachment/queued_command, line 3)", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "q1")

    def test_lagging_last_prompt_is_reported_with_its_distance(self):
        # Turns driven by teammate messages append entries but no last-prompt.
        records = [
            turn("u1", None, "user", "start the team"),
            turn("a1", "u1", "assistant", "started"),
            last_prompt("a1"),
            turn("u2", "a1", "user", "<teammate-message>done</teammate-message>"),
            turn("a2", "u2", "assistant", "noted"),
            system("t2", "a2", "turn_duration"),
        ]
        out, written = self.run_tool(records)
        self.assertIn("note: the trailing last-prompt names a1 (assistant, line 2); the leaf "
                      "below is 3 entries further along its chain, 2 of them user or assistant "
                      "entries. Grafting at the leaf keeps them on the boundary's ancestry", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "t2")

    def test_last_prompt_naming_a_missing_entry(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "u1", "assistant", "hi"),
            last_prompt("gone"),
        ]
        out, written = self.run_tool(records)
        self.assertIn("note: the trailing last-prompt names gone, which is not an entry in this file", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "a1")

    def test_last_prompt_on_another_branch(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "u1", "assistant", "first answer"),
            last_prompt("a1"),
            turn("a1b", "u1", "assistant", "rewound answer"),
        ]
        out, written = self.run_tool(records)
        self.assertIn("note: the trailing last-prompt names a1 (assistant, line 2), which is not "
                      "an ancestor of the leaf below", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "a1b")

    def test_lag_of_one_entry(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "u1", "assistant", "hi"),
            last_prompt("a1"),
            system("t1", "a1", "turn_duration"),
        ]
        out, written = self.run_tool(records)
        self.assertIn("note: the trailing last-prompt names a1 (assistant, line 2); the leaf "
                      "below is the next entry on its chain. Grafting at the leaf keeps that "
                      "entry on the boundary's ancestry", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "t1")

    def test_child_written_before_its_parent_is_followed(self):
        # The harness writes a turn_duration one line above the reply it times.
        records = [
            turn("u1", None, "user", "hello"),
            system("t1", "a1", "turn_duration"),
            turn("a1", "u1", "assistant", "hi"),
            last_prompt("t1"),
        ]
        out, written = self.run_tool(records)
        self.assertNotIn("note:", out)
        self.assertIn("logical parent:  t1 (system/turn_duration, line 2)", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "t1")

    def test_reply_written_before_its_prompt(self):
        # A "Not logged in" reply lands in the file above the prompt it answers.
        records = [
            last_prompt("a1"),
            turn("a1", "u1", "assistant", "Not logged in"),
            turn("u1", None, "user", "test"),
        ]
        out, written = self.run_tool(records)
        self.assertNotIn("note:", out)
        self.assertIn("logical parent:  a1 (assistant, line 2)", out)
        self.assertIn("1 entry further along the chain from u1 (user, line 3)", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "a1")

    def test_parent_links_that_loop_stop_the_walk(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "x1", "assistant", "hi"),
            system("x1", "a1", "turn_duration"),
        ]
        out, written = self.run_tool(records)
        self.assertIn("note: the parent links after a1 loop back to a1 (line 2); "
                      "stopping at the entry before it", out)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "x1")

    def test_sidechain_records_are_not_followed(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "u1", "assistant", "hi"),
            {**system("s1", "a1", "turn_duration"), "isSidechain": True},
        ]
        out, written = self.run_tool(records)
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "a1")

    def test_explicit_leaf_trims_and_keeps_the_record_types_it_names(self):
        records = [
            turn("u1", None, "user", "hello"),
            turn("a1", "u1", "assistant", "hi"),
            system("t1", "a1", "turn_duration"),
            turn("u2", "t1", "user", "a failed turn"),
        ]
        out, written = self.run_tool(records, "--leaf", "a1")
        self.assertIn("trim:            lines 3-4 (2 lines)", out)
        self.assertIn("logical parent:  a1 (assistant, line 2)", out)
        self.assertNotIn("entries after the last user or assistant entry", out)
        self.assertEqual([r.get("uuid") for r in written[:-2]], ["u1", "a1"])
        self.assertEqual(self.boundary(written)["logicalParentUuid"], "a1")


if __name__ == "__main__":
    unittest.main()
