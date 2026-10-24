"""Peer snapshots retain consumed metrics without writing advancing metadata."""
from copy import deepcopy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "peer_frame_statusline", Path(__file__).with_name("statusline-render.py"))
statusline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statusline)


class PeerFrameCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="statusline-peer-frame-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patches = [
            patch.object(statusline, "DB_PATH", str(self.root / "cache.db")),
            patch.object(statusline, "live_session", side_effect=lambda pid, sid, **kw:
                         {"pid": pid, "sessionId": sid, "procStart": str(pid * 100)}),
            patch.object(statusline, "process_descends_from", return_value=True),
            patch.object(statusline, "process_agent_identity", side_effect=lambda pid:
                         {"team": "team", "agent_id": "peer-%d@team" % pid,
                          "parent_session_id": "leader"}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.cache = statusline.Cache(False)
        self.addCleanup(lambda: self.cache.db.close() if self.cache.db is not None else None)

    def frame(self, index=0):
        return {
            "session_id": "peer-%d" % index,
            "transcript_path": str(self.root / ("peer-%d.jsonl" % index)),
            "model": {"id": "peer-model", "display_name": "Peer"},
            "effort": {"level": 0}, "cost": {"total_cost_usd": 2.5},
            "context_window": {
                "context_window_size": 200000, "used_percentage": 1,
                "total_input_tokens": 90000, "total_output_tokens": 1000,
                "current_usage": {
                    "input_tokens": 5000, "cache_read_input_tokens": 6000,
                    "cache_creation_input_tokens": 7000, "output_tokens": 10,
                    "iterations": [{"input_tokens": 99999, "output_tokens": 50},
                                   {"input_tokens": 1500, "cache_read_input_tokens": 400,
                                    "cache_creation_input_tokens": 100, "output_tokens": 10}],
                },
            },
            "rate_limits": {
                "five_hour": {"used_percentage": 25, "resets_at": 1800000000},
                "seven_day": {"used_percentage": 50, "resets_at": 1800600000},
            },
        }

    def register(self, frame, index=0):
        self.cache.register_frame(frame, {"pid": 100 + index, "pane": "%%%d" % (index + 1)}, 99999)

    def saved(self, frame):
        row = self.cache.db.execute("SELECT payload FROM peer_frames WHERE session_id=?",
                                    (frame["session_id"],)).fetchone()
        return json.loads(row[0])

    def test_48_peers_20_metadata_ticks_skip_writes_commits_and_json_encoding(self):
        frames = [self.frame(index) for index in range(48)]
        for index, frame in enumerate(frames):
            self.register(frame, index)
        changes = self.cache.db.total_changes
        statements = []
        self.cache.db.set_trace_callback(statements.append)
        with patch.object(statusline.json, "dumps", wraps=statusline.json.dumps) as encode:
            for tick in range(20):
                for index, frame in enumerate(frames):
                    context = frame["context_window"]
                    context["total_input_tokens"] += 100
                    context["total_output_tokens"] += 10
                    context["remaining_percentage"] = tick
                    usage = context["current_usage"]
                    usage["output_tokens"] += 10
                    usage["duration_ms"] = tick * 1000
                    usage["server_tool_use"] = {"web_search_requests": tick}
                    usage["input_tokens"] += 100  # Aggregate, not the final iteration.
                    usage["iterations"][0]["input_tokens"] += 100
                    usage["iterations"][-1]["output_tokens"] += 10
                    usage["iterations"][-1]["duration_ms"] = tick * 1000
                    frame["cost"]["total_duration_ms"] = tick * 1000
                    frame["cost"]["total_api_duration_ms"] = tick * 700
                    frame["model"]["duration_ms"] = tick
                    frame["effort"]["duration_ms"] = tick
                    frame["rate_limits"]["five_hour"]["remaining_seconds"] = 9999 - tick
                    self.register(frame, index)
            self.assertEqual(encode.call_count, 0)
        self.assertEqual(self.cache.db.total_changes, changes)
        self.assertFalse(any(sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT"))
                             for sql in statements))
        saved = self.saved(frames[0])
        self.assertEqual(saved["context_window"], {
            "context_window_size": 200000, "used_percentage": 1,
            "current_usage": {"input_tokens": 1500, "cache_read_input_tokens": 400,
                              "cache_creation_input_tokens": 100},
        })

    def test_every_consumed_metric_change_is_persisted_once(self):
        frame = self.frame()
        self.register(frame)
        changes = self.cache.db.total_changes
        updates = [
            (("model", "id"), "other-model"), (("model", "display_name"), "Other"),
            (("effort", "level"), "high"), (("cost", "total_cost_usd"), 3.75),
            (("context_window", "context_window_size"), 1000000),
            (("context_window", "used_percentage"), 7.5),
            (("context_window", "current_usage", "iterations", -1, "input_tokens"), 2000),
            (("context_window", "current_usage", "iterations", -1, "cache_read_input_tokens"), 800),
            (("context_window", "current_usage", "iterations", -1, "cache_creation_input_tokens"), 200),
            (("rate_limits", "five_hour", "used_percentage"), 35.5),
            (("rate_limits", "five_hour", "resets_at"), 1800000100),
            (("rate_limits", "seven_day", "used_percentage"), 65),
            (("rate_limits", "seven_day", "resets_at"), 1800600100),
        ]
        for path, value in updates:
            with self.subTest(path=path):
                target = frame
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                statements = []
                self.cache.db.set_trace_callback(statements.append)
                with patch.object(statusline.json, "dumps", wraps=statusline.json.dumps) as encode:
                    self.register(frame)
                    self.assertEqual(encode.call_count, 1)
                changes += 1
                self.assertEqual(self.cache.db.total_changes, changes)
                self.assertEqual(statements.count("COMMIT"), 1)
                self.assertEqual(self.saved(frame), statusline.peer_frame_fields(frame))

    def test_unknown_zero_and_last_iteration_usage_keep_the_same_render(self):
        missing = object()
        usages = [missing, None, False, [], "unknown", {},
                  {"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3},
                  {"input_tokens": 10, "iterations": [{"input_tokens": 2, "cache_read_input_tokens": 3}]},
                  {"input_tokens": 10, "iterations": [{"input_tokens": None, "cache_read_input_tokens": 3}]},
                  {"input_tokens": 10, "iterations": [{"input_tokens": 999}, {"output_tokens": 5}]},
                  {"input_tokens": 10, "iterations": [{"input_tokens": 999}, None]},
                  {"input_tokens": 10, "iterations": []}]
        view = {"agent_id": "alpha@team", "name": "Alpha"}
        self.cache.values = Mock(return_value=None)
        with patch.object(statusline, "COLOR_ENABLED", False), \
                patch.object(statusline.time, "time", return_value=1790000000):
            for usage in usages:
                with self.subTest(usage=usage):
                    frame = self.frame()
                    if usage is missing:
                        frame["context_window"].pop("current_usage")
                    else:
                        frame["context_window"]["current_usage"] = usage
                    before = deepcopy(frame)
                    projected = statusline.peer_frame_fields(frame)
                    raw_selected = statusline.selected_peer_input({}, view, frame, self.cache)
                    projected_selected = statusline.selected_peer_input({}, view, projected, self.cache)
                    self.assertEqual(statusline.render(raw_selected, self.cache),
                                     statusline.render(projected_selected, self.cache))
                    self.assertEqual(frame, before)
                    self.assertEqual(raw_selected["context_window"].get("total_input_tokens"),
                                     projected_selected["context_window"].get("total_input_tokens"))
        frame = self.frame()
        frame["context_window"]["current_usage"] = {}
        zero = statusline.peer_frame_fields(frame)
        self.assertEqual(zero["context_window"]["current_usage"], {})

    def test_snapshot_is_detached_and_retains_scalar_types(self):
        frame = self.frame()
        self.register(frame)
        snapshot = self.cache.frames[(frame["session_id"], 100, "10000")][4]
        frame["context_window"]["current_usage"]["iterations"][-1]["input_tokens"] = 9999
        self.assertEqual(snapshot["context_window"]["current_usage"]["input_tokens"], 1500)
        self.register(frame)
        self.assertEqual(self.saved(frame)["context_window"]["current_usage"]["input_tokens"], 9999)
        for value in (1, 1.0, True, None, "1"):
            frame["effort"]["level"] = value
            before = self.cache.db.total_changes
            self.register(frame)
            self.assertEqual(self.cache.db.total_changes, before + 1)
            stored = self.saved(frame)["effort"]["level"]
            self.assertIs(type(stored), type(value))
            self.assertEqual(stored, value)
        # Invalid container leaves are copied too, rather than being retained
        # by reference in an otherwise detached snapshot.
        frame["model"]["id"] = ["unexpected"]
        self.register(frame)
        snapshot = self.cache.frames[(frame["session_id"], 100, "10000")][4]
        frame["model"]["id"].append("caller-edit")
        self.assertEqual(snapshot["model"]["id"], ["unexpected"])


if __name__ == "__main__":
    unittest.main()
