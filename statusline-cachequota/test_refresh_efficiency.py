"""Shared transcript presentations reuse work without weakening peer checks."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


SPEC = importlib.util.spec_from_file_location(
    "refresh_statusline", Path(__file__).with_name("statusline-render.py"))
statusline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statusline)
ACTIVATION = "v2_" + "a" * 32


class PresentationMemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="statusline-memo-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transcript = self.root / "peer.jsonl"
        self.transcript.write_text("")
        self.patches = [
            patch.object(statusline, "CLAUDE_DIR", str(self.root)),
            patch.dict(statusline.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
            patch.object(statusline.time, "time", return_value=1_792_848_000),
        ]
        self.clock = None
        for item in self.patches:
            self.clock = item.start()
            self.addCleanup(item.stop)
        statusline._display_profile.cache_clear()
        self.peer = {
            "session_id": "peer", "transcript_path": str(self.transcript),
            "model": {"id": "peer-model", "display_name": "Peer"},
            "cost": {"total_cost_usd": 2.5},
            "context_window": {"current_usage": {"input_tokens": 10000},
                               "total_input_tokens": 10000,
                               "context_window_size": 200000, "used_percentage": 5},
            "rate_limits": {"five_hour": {"used_percentage": 31,
                                           "resets_at": 1_792_851_600}},
        }
        self.cache = statusline.Cache(False)
        self.cache.register_frame = Mock()
        self.cache.values = Mock(return_value=None)
        self.cache.peer_frame = Mock(return_value=self.peer)
        self.memo = {}

    def request(self, selected=False, display=None):
        native = ({"session_id": "viewer",
                   "transcript_path": str(self.root / "viewer.jsonl")}
                  if selected else self.peer)
        request = {"statusline_input": native,
                   "statusline_display": display if display is not None else {"NO_COLOR": ""}}
        if selected:
            request.update(statusline_activation=ACTIVATION, statusline_view={
                "version": 2, "activation": ACTIVATION, "session_id": "viewer",
                "agent_id": "alpha@team", "teammate_id": "alpha@team", "name": "Alpha"})
        return request

    def show(self, request):
        return statusline.serve(request, self.cache, self.memo)

    def test_own_pane_and_selected_peer_reuse_distinct_presentations(self):
        own, selected = self.request(), self.request(selected=True)
        with patch.object(statusline, "render_core", wraps=statusline.render_core) as core:
            own_line, selected_line = self.show(own), self.show(selected)
            self.assertNotIn("agent Alpha", own_line)
            self.assertIn("agent Alpha", selected_line)
            self.assertNotIn("session $", own_line)
            self.assertIn("session $2.50", selected_line)
            for _ in range(10):
                self.assertEqual(self.show(own), own_line)
                self.assertEqual(self.show(selected), selected_line)
            self.assertEqual(core.call_count, 2)
            self.assertEqual(len(self.memo), 2)

    def test_48_requests_sharing_one_transcript_reuse_all_four_profiles(self):
        profiles = [
            {"COLORTERM": "truecolor"}, {"NO_COLOR": ""}, {},
            {"COLORTERM": "truecolor", "CLAUDE_STATUSLINE_THEME": "light"},
        ]
        requests = [self.request(selected=bool(index // 4 % 2), display=profiles[index % 4])
                    for index in range(48)]
        with patch.object(statusline, "_display_values", wraps=statusline._display_values) as parse, \
                patch.object(statusline, "render_core", wraps=statusline.render_core) as core:
            lines = [self.show(request) for request in requests]
            self.assertEqual(core.call_count, 8)
            self.assertEqual(parse.call_count, 4)
            for cycle in range(20):
                for index in range(48):
                    at = (index + cycle) % len(requests)
                    self.assertEqual(self.show(requests[at]), lines[at])
            self.assertEqual(core.call_count, 8)
            self.assertEqual(parse.call_count, 4)
            self.assertEqual(len(self.memo), 8)

    def test_each_presentation_revalidates_metrics_and_transcript_but_refills_clock(self):
        requests = [self.request(), self.request(selected=True)]
        with patch.object(statusline, "render_core", wraps=statusline.render_core) as core:
            for request in requests:
                self.assertIn("(1:00:00)", self.show(request))
            self.assertEqual(core.call_count, 2)
            self.peer["model"] = {"id": "updated-model", "display_name": "Updated"}
            for request in requests:
                self.assertIn("Updated", self.show(request))
            self.assertEqual(core.call_count, 4)
            self.transcript.write_text('{"type":"progress"}\n')
            for request in requests:
                self.show(request)
            self.assertEqual(core.call_count, 6)
            self.clock.return_value += 61
            for request in requests:
                self.assertIn("(0:58:59)", self.show(request))
            self.assertEqual(core.call_count, 6)

    def test_memo_is_bounded_and_refreshes_lru_position_on_a_hit(self):
        requests = [self.request(display={"COLORTERM": "truecolor",
                                         "CLAUDE_STATUSLINE_MUTED_RGB": "%d,1,2" % index})
                    for index in range(4)]
        with patch.object(statusline, "RENDER_MEMO_SIZE", 3), \
                patch.object(statusline, "render_core", wraps=statusline.render_core) as core:
            lines = [self.show(request) for request in requests[:3]]
            self.assertEqual(self.show(requests[0]), lines[0])
            self.assertEqual(core.call_count, 3)
            self.show(requests[3])
            self.assertEqual(len(self.memo), 3)
            self.assertEqual(core.call_count, 4)
            self.assertEqual(self.show(requests[0]), lines[0])
            self.assertEqual(core.call_count, 4)
            self.assertEqual(self.show(requests[1]), lines[1])
            self.assertEqual(core.call_count, 5)
            self.assertEqual(len(self.memo), 3)


class PeerLookupReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="statusline-peer-reads-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = {"name": "team", "leadSessionId": "leader", "members": [
            {"agentId": "alpha@team", "name": "alpha", "tmuxPaneId": "%1", "backendType": "tmux"},
            {"agentId": "beta@team", "name": "beta", "tmuxPaneId": "%2", "backendType": "tmux"},
        ]}
        for item in (patch.object(statusline, "DB_PATH", str(self.root / "cache.db")),
                     patch.object(statusline, "read_json_object", return_value=self.roster)):
            item.start()
            self.addCleanup(item.stop)
        self.cache = statusline.Cache(False)
        self.cache.db = statusline.open_db()
        self.addCleanup(self.cache.db.close)
        self.records = {}

    def peer(self, pid=101, sid="peer", pane="%1", name="alpha", parent="leader"):
        start = str(pid * 100)
        frame = {"session_id": sid, "transcript_path": str(self.root / (sid + ".jsonl")),
                 "model": {"id": name + "-model"}}
        self.cache.db.execute("INSERT INTO peer_frames VALUES(?,?,?,?,?,?,?,?,?)", (
            sid, pid, start, "team", name + "@team", parent, pane, json.dumps(frame), 1))
        self.records[pid] = {"pid": pid, "sessionId": sid, "procStart": start,
                             "tmux": "swarm:@0." + pane}
        return frame

    def live(self, pid, sid, expected_start=None, allow_missing=False):
        record = self.records.get(pid)
        return record if (record and record["sessionId"] == sid
                          and expected_start in (None, record["procStart"])) else None

    def test_qualified_lead_does_not_read_its_unused_session_record(self):
        frame = self.peer()
        with patch.object(statusline, "live_session", side_effect=self.live) as live:
            self.assertEqual(self.cache.peer_frame({"session_id": "leader"}, {"pid": 11}, "alpha@team"), frame)
            self.assertEqual(live.call_args_list, [call(101, "peer", "10100", allow_missing=True)])

    def test_retired_panes_skip_process_and_session_checks(self):
        self.peer(pid=90, sid="old-one", pane="%90")
        self.peer(pid=91, sid="old-two", pane="%91")
        frame = self.peer()
        with patch.object(statusline, "live_session", side_effect=self.live) as live:
            self.assertEqual(self.cache.peer_frame({"session_id": "leader"}, {"pid": 11}, "alpha@team"), frame)
            self.assertEqual(live.call_args_list, [call(101, "peer", "10100", allow_missing=True)])

    def test_nonlead_viewer_still_requires_its_current_membership(self):
        frame = self.peer()
        self.peer(pid=201, sid="viewer", pane="%2", name="beta")
        with patch.object(statusline, "live_session", side_effect=self.live) as live:
            self.assertEqual(self.cache.peer_frame({"session_id": "viewer"}, {"pid": 201}, "alpha@team"), frame)
            self.assertEqual(live.call_args_list, [call(201, "viewer", allow_missing=True),
                                                  call(101, "peer", "10100", allow_missing=True)])

    def test_current_pane_still_requires_a_live_matching_process(self):
        self.peer()
        self.records.clear()
        with patch.object(statusline, "live_session", side_effect=self.live) as live:
            self.assertIsNone(self.cache.peer_frame({"session_id": "leader"}, {}, "alpha@team"))
            self.assertEqual(live.call_count, 1)

    def test_multiple_live_processes_on_current_pane_remain_ambiguous(self):
        self.peer()
        self.peer(pid=102, sid="duplicate", pane="%1")
        with patch.object(statusline, "live_session", side_effect=self.live) as live:
            self.assertIsNone(self.cache.peer_frame({"session_id": "leader"}, {}, "alpha@team"))
            self.assertEqual(live.call_count, 2)


if __name__ == "__main__":
    unittest.main()
