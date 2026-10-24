import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "selected_statusline", Path(__file__).with_name("statusline-render.py"))
statusline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statusline)


def response(uuid, parent, model, read, written, agent=None, mid=None, ttl="5m"):
    row = {
        "uuid": uuid, "parentUuid": parent, "type": "assistant",
        "timestamp": "2026-10-24T10:00:00Z", "isSidechain": agent is not None,
        "message": {"id": mid or uuid, "model": model, "usage": {
            "input_tokens": 0, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": written, "output_tokens": 12,
            "cache_creation": {"ephemeral_" + ttl + "_input_tokens": written},
        }},
    }
    if agent is not None:
        row["agentId"] = agent
    return row


class SelectedAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="statusline-test-")
        self.root = Path(self.tmp.name)
        self.main = self.root / "session-main.jsonl"
        self.agents = self.root / "session-main" / "subagents"
        self.agents.mkdir(parents=True)
        self.patches = [
            patch.object(statusline, "CLAUDE_DIR", str(self.root)),
            patch.object(statusline, "DB_PATH", str(self.root / "cache.db")),
            patch.object(statusline, "SIDECAR", str(self.root / "memo.json")),
            patch.object(statusline, "COLOR_ENABLED", False),
            patch.object(statusline, "TRUECOLOR", False),
            patch.object(statusline, "COMPACT", False),
        ]
        for item in self.patches:
            item.start()
        self.cache = statusline.Cache(False)
        self.memo = {}
        self.write(self.main, response("m1", None, "main-model", 80000, 15000, ttl="1h"))
        self.write(self.agent("a1"), response("a1-first", None, "model-a", 18000, 2000, "a1"))
        self.write(self.agent("b1"), response("b1-first", None, "model-b", 60000, 10000, "b1"))
        self.native = {
            "session_id": "session-main", "transcript_path": str(self.main),
            "model": {"id": "main-model", "display_name": "Main Model"},
            "effort": {"level": "max"},
            "context_window": {"total_input_tokens": 95000, "context_window_size": 1000000,
                               "used_percentage": 9.5},
            "cost": {"total_cost_usd": 5.25},
            "rate_limits": {"five_hour": {"used_percentage": 23}},
        }

    def tearDown(self):
        if self.cache.db is not None:
            self.cache.db.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def agent(self, agent_id):
        return self.agents / ("agent-" + agent_id + ".jsonl")

    def write(self, path, *rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def append(self, path, *rows):
        with path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def envelope(self, agent="a1", **fields):
        view = {"version": 1, "session_id": "session-main", "agent_id": agent, **fields}
        return {"statusline_input": self.native, "statusline_view": json.dumps(view)}

    def show(self, data):
        return statusline.serve(data, self.cache, self.memo)

    def feed(self, tasks, native=None):
        return self.show({"statusline_input": {**(native or self.native), "tasks": tasks},
                          "statusline_view": "", "statusline_task_metadata": True})

    def test_switch_main_agents_and_back(self):
        main = self.show(self.native)
        first = self.show(self.envelope("a1", name="Alpha"))
        second = self.show(self.envelope("b1", name="Beta"))
        self.assertIn("Main Model", main)
        self.assertIn("agent Alpha", first)
        self.assertIn("model-a", first)
        self.assertIn("ctx 20k", first)
        self.assertIn("session $5.25", first)
        self.assertIn("5h", first)
        self.assertNotIn("effort:max", first)
        self.assertNotIn("95k", first)
        self.assertIn("model-b", second)
        self.assertIn("ctx 70k", second)
        self.assertEqual(main, self.show(self.envelope(None)))
        self.assertEqual(main, self.show(self.native))

    def test_invalid_envelopes_keep_native(self):
        main = self.show(self.native)
        for view in ("invalid", "null", "[]", "{}", json.dumps({"version": 9}),
                     json.dumps({"version": 1, "session_id": "other", "agent_id": "a1"})):
            with self.subTest(view=view):
                self.assertEqual(main, self.show({"statusline_input": self.native,
                                                 "statusline_view": view}))
        for agent in ("../a1", "a1/../../secrets", "a1\n", "", "a1.jsonl", 42):
            self.assertEqual(main, self.show(self.envelope(agent)))

    def test_session_id_can_come_from_transcript_filename(self):
        self.native.pop("session_id")
        self.assertIn("model-a", self.show(self.envelope()))

    def test_missing_transcript_does_not_show_parent_metrics(self):
        text = self.show(self.envelope("missing", name="Missing"))
        self.assertIn("agent Missing", text)
        self.assertIn("transcript unavailable", text)
        for value in ("Main Model", "ctx", "cache", "effort:max", "95k"):
            self.assertNotIn(value, text)
        self.assertIn("session $5.25", text)

    def test_selection_is_per_request_and_per_session(self):
        self.show(self.envelope())
        other = dict(self.native, session_id="second")
        self.assertEqual(self.show(self.native), self.show(other))
        self.assertIn("model-a", self.show(self.envelope()))

    def test_main_still_skips_sidechains(self):
        self.append(self.main, response("foreign", "m1", "wrong-model", 0, 900000, "a1"))
        values = self.cache.values(str(self.main))
        self.assertEqual(values[6:], (95000, "main-model"))

    def test_agent_file_excludes_another_agent(self):
        self.append(self.agent("a1"), response("foreign", "a1-first", "wrong", 0, 800000, "b1"))
        text = self.show(self.envelope())
        self.assertIn("ctx 20k", text)
        self.assertNotIn("wrong", text)

    def test_uuid_state_is_owned_by_transcript(self):
        a, b = str(self.agent("a1")), str(self.agent("b1"))
        self.write(Path(a), response("shared", None, "model-a", 0, 10000, "a1"))
        self.write(Path(b), response("shared", None, "model-b", 0, 100000, "b1"))
        self.cache.values(a, "a1")
        self.cache.values(b, "b1")
        self.append(Path(a), response("a2", "shared", "model-a", 10000, 10000, "a1"))
        values = self.cache.values(a, "a1")
        self.assertEqual(values[2], 0)
        self.assertEqual(self.cache.db.execute("SELECT count(*) FROM nodes WHERE uuid='shared'").fetchone()[0], 2)

    def test_append_and_partial_tail_refresh_selected_data(self):
        first = self.show(self.envelope())
        row = response("a2", "a1-first", "model-a", 20000, 10000, "a1")
        raw = json.dumps(row)
        with self.agent("a1").open("a") as stream:
            stream.write(raw[:len(raw)//2])
        self.assertEqual(first, self.show(self.envelope()))
        with self.agent("a1").open("a") as stream:
            stream.write(raw[len(raw)//2:] + "\n")
        self.assertIn("ctx 30k", self.show(self.envelope()))

    def test_midstream_and_duplicate_message_do_not_count_as_new_calls(self):
        path = self.agent("a1")
        partial = response("partial", "a1-first", "model-a", 0, 0, "a1", "message-2")
        partial["message"]["usage"] = {"input_tokens": 2}
        self.append(path, partial)
        self.assertIn("ctx 20k", self.show(self.envelope()))
        self.append(path, response("final", "partial", "model-a", 20000, 5000, "a1", "message-2"),
                    response("duplicate", "final", "model-a", 20000, 5000, "a1", "message-2"))
        self.assertIn("ctx 25k", self.show(self.envelope()))
        count = self.cache.db.execute("SELECT total FROM nodes WHERE path=? AND uuid='duplicate'", (str(path),)).fetchone()[0]
        self.assertEqual(count, 2)

    def test_compaction_clears_live_context_until_next_response(self):
        self.show(self.envelope())
        self.append(self.agent("a1"), {"uuid": "compact", "parentUuid": "a1-first",
            "type": "system", "subtype": "compact_boundary", "isSidechain": True, "agentId": "a1"})
        text = self.show(self.envelope())
        self.assertNotIn("ctx", text)
        self.assertNotIn("cache", text)

    def test_replacement_drops_old_nodes(self):
        self.show(self.envelope())
        other = self.root / "replacement.jsonl"
        self.write(other, response("new", "a1-first", "new-model", 0, 30000, "a1"))
        os.replace(other, self.agent("a1"))
        text = self.show(self.envelope())
        self.assertIn("new-model", text)
        self.assertIn("ctx 30k", text)
        self.assertIn("recached 0.00x", text)

    def test_task_metadata_supplies_exact_agent_limit(self):
        self.assertEqual("", self.feed([{"id": "a1", "model": "model-a[1m]", "effort": "high", "contextWindowSize": 1000000}]))
        text = self.show(self.envelope())
        self.assertIn("20k/1.0M", text)
        self.assertIn("2%", text)
        self.assertIn("effort:high", text)
        self.feed([{"id": "a1", "model": "different-model", "contextWindowSize": 200000}])
        self.assertIn("ctx 20k", self.show(self.envelope()))
        self.assertNotIn("20k/", self.show(self.envelope()))

    def test_task_metadata_does_not_cross_sessions_or_transcript_paths(self):
        task = {"id": "a1", "model": "model-a", "contextWindowSize": 200000}
        self.feed([task], dict(self.native, session_id="other"))
        self.feed([task], dict(self.native, transcript_path=str(self.root / "other.jsonl")))
        self.assertNotIn("20k/", self.show(self.envelope()))

    def test_metadata_survives_renderer_restart(self):
        self.feed([{"id": "a1", "model": "model-a", "contextWindowSize": 200000}])
        self.cache.db.close()
        self.cache = statusline.Cache(False)
        self.assertIn("20k/200k", self.show(self.envelope()))

    def test_same_main_model_never_lends_its_context_limit(self):
        self.native["model"]["id"] = "model-a"
        self.assertNotIn("20k/", self.show(self.envelope()))

    def test_explicit_window_and_current_effort(self):
        text = self.show(self.envelope(model="model-a", effort="low", context_window_size=200000))
        self.assertIn("20k/200k", text)
        self.assertIn("effort:low", text)

    def test_owner_lock_release_returns_to_main_without_data_change(self):
        import fcntl
        owner = self.root / "statusline-view-ABC123"
        fd = os.open(owner, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            envelope = self.envelope(owner=owner.name)
            self.assertIn("model-a", self.show(envelope))
        finally:
            os.close(fd)
        self.assertEqual(self.show(self.native), self.show(envelope))
        for name in ("../statusline-view-ABC123", "absent", "statusline-view-MISSING"):
            self.assertEqual(self.show(self.native), self.show(self.envelope(owner=name)))

    def test_expired_lease_returns_to_main_before_memo_lookup(self):
        with patch.object(statusline.time, "time", return_value=100):
            envelope = self.envelope(expires_at=100500)
            self.assertIn("model-a", self.show(envelope))
        with patch.object(statusline.time, "time", return_value=101):
            self.assertEqual(self.show(self.native), self.show(envelope))
        for expiry in (float("nan"), float("inf"), "later", -1, True):
            self.assertEqual(self.show(self.native), self.show(self.envelope(expires_at=expiry)))

    def test_names_are_safe_and_visible_in_compact_mode(self):
        with patch.object(statusline, "COMPACT", True):
            text = self.show(self.envelope(name="Alpha\n\x1b\x00\u202eB"))
        self.assertIn("agent AlphaB", text)
        for char in ("\n", "\x1b", "\x00", "\u202e"):
            self.assertNotIn(char, text)

    def test_selected_five_minute_badge_is_muted(self):
        with patch.object(statusline, "COLOR_ENABLED", True), patch.object(statusline, "TRUECOLOR", True):
            text = self.show(self.envelope())
            self.assertIn(statusline.muted("5m"), text)
            self.assertNotIn(statusline.paint("5m", fg=statusline.ALARM_RGB), text)

    def test_old_persistent_memo_is_ignored(self):
        path = self.agent("a1")
        st = path.stat()
        Path(statusline.SIDECAR).write_text(json.dumps({str(path): [st.st_size, st.st_mtime_ns, None]}))
        cache = statusline.Cache(True)
        self.assertIn("ctx 20k", statusline.render(self.envelope(), cache))
        if cache.db is not None:
            cache.db.close()

    def test_context_uses_last_server_tool_iteration(self):
        row = response("a2", "a1-first", "model-a", 60000, 20000, "a1")
        row["message"]["usage"]["iterations"] = [
            {"input_tokens": 0, "cache_read_input_tokens": 20000, "cache_creation_input_tokens": 10000},
            {"input_tokens": 0, "cache_read_input_tokens": 40000, "cache_creation_input_tokens": 10000},
        ]
        self.append(self.agent("a1"), row)
        self.assertIn("ctx 50k", self.show(self.envelope()))


if __name__ == "__main__":
    unittest.main()
