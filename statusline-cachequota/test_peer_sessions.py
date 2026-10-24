"""Cross-process selection uses current native identity and raw peer metrics."""
import io
import hashlib
import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_selected_agent import ACTIVATION, response, statusline


class PeerSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="statusline-peers-")
        self.root = Path(self.tmp.name)
        (self.root / "sessions").mkdir()
        (self.root / "teams" / "team").mkdir(parents=True)
        self.starts = {}
        self.identities = {}
        self.patches = [
            patch.object(statusline, "CLAUDE_DIR", str(self.root)),
            patch.object(statusline, "DB_PATH", str(self.root / "cache.db")),
            patch.object(statusline, "SIDECAR", str(self.root / "memo.json")),
            patch.object(statusline, "COLOR_ENABLED", False),
            patch.object(statusline, "TRUECOLOR", False),
            patch.object(statusline, "COMPACT", False),
            patch.object(statusline, "process_start", side_effect=lambda pid: self.starts.get(pid)),
            patch.object(statusline, "process_descends_from", side_effect=lambda origin, pid, start:
                         origin == 99999 and self.starts.get(pid) == start),
            patch.object(statusline, "process_agent_identity", side_effect=lambda pid: self.identities.get(pid, {})),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.cache = statusline.Cache(False)
        self.memo = {}
        self.native = {"session_id": "leader", "transcript_path": str(self.root / "leader.jsonl"),
                       "model": {"id": "leader-model", "display_name": "Leader Model"},
                       "effort": {"level": "max"}, "cost": {"total_cost_usd": 99},
                       "context_window": {"total_input_tokens": 95000, "context_window_size": 1000000,
                                          "used_percentage": 9.5},
                       "rate_limits": {"five_hour": {"used_percentage": 23}}}
        self.roster()

    def tearDown(self):
        if self.cache.db is not None:
            self.cache.db.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def roster(self, lead="leader", alpha_pane="%1", extra=()):
        self.roster_data = {"name": "team", "leadSessionId": lead, "members": [
            {"agentId": "alpha@team", "name": "alpha", "tmuxPaneId": alpha_pane, "backendType": "tmux"},
            {"agentId": "beta@team", "name": "beta", "tmuxPaneId": "%2", "backendType": "tmux"},
            *extra,
        ]}
        self.save_roster()

    def save_roster(self):
        (self.root / "teams" / "team" / "config.json").write_text(json.dumps(self.roster_data))

    def peer(self, pid=101, sid="peer-alpha", name="alpha", pane="%1", used=10000,
             model="alpha-model", cost=2.5, start=None, register=True):
        start = start or str(pid * 100)
        self.starts[pid] = start
        self.identities[pid] = {"agent_id": name + "@team", "name": name,
                                "team": "team", "parent_session_id": "leader"}
        record = {"pid": pid, "sessionId": sid, "procStart": start, "tmux": "swarm:@0." + pane}
        (self.root / "sessions" / (str(pid) + ".json")).write_text(json.dumps(record))
        transcript = self.root / (sid + ".jsonl")
        transcript.write_text(json.dumps(response("root", None, model, 0, used, ttl="1h")) + "\n")
        native = {"session_id": sid, "transcript_path": str(transcript),
                  "model": {"id": model, "display_name": "Peer " + name.title()},
                  "effort": {"level": "low"}, "cost": {"total_cost_usd": cost},
                  "context_window": {"total_input_tokens": used * 10, "context_window_size": 200000,
                                     "used_percentage": used / 2000,
                                     "current_usage": {"input_tokens": used, "output_tokens": 12}}}
        envelope = {"statusline_input": native, "statusline_process": {"pid": pid, "pane": pane}}
        if register:
            self.show(envelope)
        return envelope

    def selection(self, name="alpha", native=None, process=None):
        native = native or self.native
        view = {"version": 2, "activation": ACTIVATION, "session_id": native["session_id"],
                "agent_id": name + "@team", "teammate_id": name + "@team", "name": name.title()}
        return {"statusline_input": native, "statusline_view": json.dumps(view),
                "statusline_activation": ACTIVATION, "statusline_process": process or {}}

    def show(self, envelope, origin_pid=99999):
        return statusline.serve(envelope, self.cache, self.memo, origin_pid=origin_pid)

    def test_select_two_peer_processes_and_return_to_main(self):
        self.peer()
        self.peer(pid=102, sid="peer-beta", name="beta", pane="%2", used=40000,
                  model="beta-model", cost=7)
        alpha = self.show(self.selection())
        beta = self.show(self.selection("beta"))
        self.assertIn("agent Alpha", alpha)
        self.assertIn("Peer Alpha", alpha)
        self.assertIn("10k/200k", alpha)
        self.assertIn("effort:low", alpha)
        self.assertIn("session $2.50", alpha)
        self.assertIn("40k/200k", beta)
        self.assertIn("session $7.00", beta)
        for line in (alpha, beta):
            self.assertNotIn("Leader Model", line)
            self.assertNotIn("95k", line)
            self.assertNotIn("$99", line)
            self.assertNotIn("5h", line)
        self.assertIn("Leader Model", self.show({"statusline_input": self.native}))

    def test_unavailable_peer_keeps_selected_label_without_parent_metrics(self):
        line = self.show(self.selection())
        self.assertIn("agent Alpha", line)
        self.assertIn("teammate unavailable", line)
        for text in ("Leader Model", "ctx", "cache", "effort:max", "$99", "5h", "7d"):
            self.assertNotIn(text, line)

    def test_peer_quotas_and_reset_times_belong_to_each_resolved_peer(self):
        self.native["rate_limits"] = {
            "five_hour": {"used_percentage": 23, "resets_at": 1700000100},
            "seven_day": {"used_percentage": 65, "resets_at": 1700000200}}
        alpha = self.peer(register=False)
        beta = self.peer(pid=102, sid="peer-beta", name="beta", pane="%2", model="beta-model", register=False)
        for name, frame, five, week, reset in (("alpha", alpha, 11, 37, 1800000000),
                                                ("beta", beta, 44, 68, 1900000000)):
            limits = {"five_hour": {"used_percentage": five, "resets_at": reset},
                      "seven_day": {"used_percentage": week, "resets_at": reset + 600000}}
            frame["statusline_input"]["rate_limits"] = limits
            self.show(frame)
            template, slots = statusline.render_core(self.selection(name), self.cache, origin_pid=99999)
            line = statusline.fill(template, slots)
            self.assertIn("%d%%" % five, line)
            self.assertIn("%d%%" % week, line)
            self.assertNotIn("23%", line)
            self.assertNotIn("65%", line)
            self.assertIn(("reset_rel", reset), slots)
            self.assertIn(("reset_abs", reset + 600000), slots)
            self.assertNotIn(("reset_rel", 1700000100), slots)
            self.assertNotIn(("reset_abs", 1700000200), slots)

    def test_absent_or_unavailable_peer_quotas_do_not_fall_back_to_viewer(self):
        self.peer()
        for name in ("alpha", "beta"):
            with self.subTest(name=name):
                selected = statusline.selected_input(self.selection(name), self.cache, origin_pid=99999)
                self.assertEqual(selected["rate_limits"], {})
                line = self.show(self.selection(name))
                self.assertNotIn("5h", line)
                self.assertNotIn("7d", line)

    def test_daemon_token_and_shared_proxy_cannot_replace_peer_quota_or_add_org(self):
        token = "fake-statusline-test-token"
        directory = self.root / "proxy-state"
        directory.mkdir()
        proxy = directory / (hashlib.sha256(("Bearer " + token).encode()).hexdigest()[:16] + ".json")
        proxy.write_text(json.dumps({"org": "daemon-org", "h5": .91, "d7": .89}))
        credentials = self.root / ".credentials.json"
        credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
        peer = self.peer(register=False)
        peer["statusline_input"]["rate_limits"] = {
            "five_hour": {"used_percentage": 12}, "seven_day": {"used_percentage": 34}}
        self.show(peer)
        for source in ("environment", "shared-credentials"):
            with self.subTest(source=source), patch.dict(os.environ):
                if source == "environment":
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
                else:
                    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                # Main rendering keeps its existing proxy behavior and primes
                # the daemon cache before the person switches to a peer.
                self.memo.clear()
                main = self.show(self.native)
                self.assertIn("org daemon", main)
                self.assertIn("91%", main)
                with patch("builtins.open", wraps=open) as opened:
                    line = self.show(self.selection())
                    missing = self.show(self.selection("beta"))
                self.assertIn("12%", line)
                self.assertIn("34%", line)
                for value in ("org ", "91%", "89%"):
                    self.assertNotIn(value, line)
                    self.assertNotIn(value, missing)
                self.assertNotIn("5h", missing)
                self.assertNotIn("7d", missing)
                reads = [str(call.args[0]) for call in opened.call_args_list if call.args]
                self.assertNotIn(str(credentials), reads)
                self.assertNotIn(str(proxy), reads)

    def test_raw_peer_frame_registered_before_its_own_view_and_tasks_do_not_overwrite(self):
        peer = self.peer(register=False)
        peer["statusline_activation"] = ACTIVATION
        peer["statusline_view"] = json.dumps({"version": 2, "activation": ACTIVATION,
            "session_id": "peer-alpha", "agent_id": "grandchild", "name": "Grandchild"})
        self.assertIn("Grandchild", self.show(peer))
        self.assertIn("Peer Alpha", self.show(self.selection()))
        partial = {"session_id": "peer-alpha", "transcript_path": peer["statusline_input"]["transcript_path"],
                   "tasks": [{"id": "grandchild", "model": "grandchild-model"}]}
        self.assertEqual(self.show({"statusline_input": partial, "statusline_task_metadata": True,
                                   "statusline_process": peer["statusline_process"]}), "")
        self.assertIn("10k/200k", self.show(self.selection()))

    def test_latest_transcript_overrides_old_frame_and_model_limit(self):
        peer = self.peer()
        path = Path(peer["statusline_input"]["transcript_path"])
        with path.open("a") as stream:
            stream.write(json.dumps(response("next", "root", "alpha-model", 10000, 10000, ttl="1h")) + "\n")
        self.assertIn("20k/200k", self.show(self.selection()))
        with path.open("a") as stream:
            stream.write(json.dumps(response("changed", "next", "new-model", 20000, 15000, ttl="1h")) + "\n")
        line = self.show(self.selection())
        self.assertIn("new-model", line)
        self.assertIn("ctx 35k", line)
        for text in ("/200k", "effort:low", "Peer Alpha"):
            self.assertNotIn(text, line)

    def test_dead_process_and_reused_pid_do_not_resolve_old_snapshot(self):
        self.peer()
        self.starts.pop(101)
        self.assertIn("teammate unavailable", self.show(self.selection()))
        self.starts[101] = "new-start"
        self.assertIn("teammate unavailable", self.show(self.selection()))
        self.peer(sid="reused-alpha", start="22222", used=30000)
        self.assertIn("30k/200k", self.show(self.selection()))
        self.assertEqual(self.mocks[-1].call_count, 2)

    def test_clear_changes_session_without_pid_change_and_rejects_old_frame(self):
        old = self.peer()
        record_path = self.root / "sessions" / "101.json"
        record = json.loads(record_path.read_text())
        record["sessionId"] = "cleared"
        record_path.write_text(json.dumps(record))
        self.show(old)
        self.assertIn("teammate unavailable", self.show(self.selection()))
        self.peer(sid="cleared", used=50000)
        self.assertIn("50k/200k", self.show(self.selection()))
        self.assertEqual(self.cache.db.execute("SELECT count(*) FROM peer_frames WHERE pid=101").fetchone()[0], 1)
        self.assertEqual(self.mocks[-1].call_count, 1)

    def test_roster_replacement_and_ambiguous_live_peers_never_pick_newest(self):
        self.peer()
        self.peer(pid=103, sid="respawn", pane="%3", used=60000)
        self.assertIn("10k/200k", self.show(self.selection()))
        self.roster(alpha_pane="%3")
        self.assertIn("60k/200k", self.show(self.selection()))
        self.peer(pid=104, sid="ambiguous", pane="%3", used=70000)
        self.assertIn("teammate unavailable", self.show(self.selection()))

    def test_pending_respawn_without_pane_does_not_select_previous_process(self):
        self.peer()
        self.roster(alpha_pane="")
        self.assertIn("teammate unavailable", self.show(self.selection()))

    def test_same_session_resumed_by_two_processes_stays_ambiguous(self):
        self.peer()
        self.peer(pid=105)
        self.assertEqual(self.cache.db.execute("SELECT count(*) FROM peer_frames"
                                               " WHERE session_id='peer-alpha'").fetchone()[0], 2)
        self.assertIn("teammate unavailable", self.show(self.selection()))

    def test_peer_five_minute_cache_badge_keeps_main_session_semantics(self):
        peer = self.peer(register=False)
        Path(peer["statusline_input"]["transcript_path"]).write_text(
            json.dumps(response("root", None, "alpha-model", 0, 10000, ttl="5m")) + "\n")
        self.show(peer)
        with patch.object(statusline, "COLOR_ENABLED", True), patch.object(statusline, "TRUECOLOR", True):
            line = self.show(self.selection())
            self.assertIn(statusline.paint("5m", fg=statusline.ALARM_RGB), line)

    def test_wrong_team_viewer_is_rejected_and_roster_member_can_view_sibling(self):
        self.peer()
        self.roster(lead="different-leader")
        foreign = dict(self.native, session_id="unrelated-viewer")
        self.assertIn("teammate unavailable", self.show(self.selection(native=foreign)))
        beta = self.peer(pid=102, sid="peer-beta", name="beta", pane="%2", model="beta-model")
        self.assertIn("Peer Alpha", self.show(self.selection(native=beta["statusline_input"],
                                                              process=beta["statusline_process"])))

    def test_parent_session_is_authoritative_when_roster_leader_is_absent_or_stale(self):
        self.peer()
        for lead in ("", "previous-leader-session"):
            with self.subTest(lead=lead):
                self.roster(lead=lead)
                self.assertIn("Peer Alpha", self.show(self.selection()))
                foreign = dict(self.native, session_id="foreign")
                self.assertIn("teammate unavailable", self.show(self.selection(native=foreign)))

    def test_normalized_team_directory_keeps_exact_roster_and_address_checks(self):
        peer = self.peer(register=False)
        team = "Research_Team"
        address = "alpha@" + team
        self.identities[101].update(team=team, agent_id=address)
        directory = self.root / "teams" / "research-team"
        directory.mkdir()
        roster = {"name": team, "leadSessionId": "leader", "members": [
            {"agentId": address, "name": "alpha", "tmuxPaneId": "%1", "backendType": "tmux"}]}
        path = directory / "config.json"
        path.write_text(json.dumps(roster))
        self.show(peer)
        selected = self.selection()
        view = json.loads(selected["statusline_view"])
        view.update(agent_id=address, teammate_id=address)
        selected["statusline_view"] = json.dumps(view)
        self.assertFalse((self.root / "teams" / team).exists())
        self.assertIn("Peer Alpha", self.show(selected))
        # Another spelling can normalize to the same directory. It must not
        # match a different logical team or another full teammate address.
        roster["name"] = "Research-Team"
        path.write_text(json.dumps(roster))
        self.assertIn("teammate unavailable", self.show(selected))
        roster["name"] = team
        roster["members"][0]["agentId"] = "alpha@Research-Team"
        path.write_text(json.dumps(roster))
        self.assertIn("teammate unavailable", self.show(selected))

    def test_address_does_not_override_an_inprocess_roster_member(self):
        self.peer()
        self.roster_data["members"][0]["backendType"] = "in-process"
        self.save_roster()
        self.assertIn("teammate unavailable", self.show(self.selection()))

    def test_registry_survives_daemon_restart_but_revalidates_live_session(self):
        self.peer()
        self.cache.db.close()
        self.cache = statusline.Cache(False)
        self.assertIn("Peer Alpha", self.show(self.selection()))
        path = self.root / "sessions" / "101.json"
        record = json.loads(path.read_text())
        record["sessionId"] = "other-session"
        path.write_text(json.dumps(record))
        self.assertIn("teammate unavailable", self.show(self.selection()))

    def test_external_peer_without_native_ledger_uses_verified_ancestry(self):
        peer = self.peer(register=False)
        (self.root / "sessions" / "101.json").unlink()
        self.show(peer)
        self.assertIn("Peer Alpha", self.show(self.selection()))
        self.cache.db.close()
        self.cache = statusline.Cache(False)
        self.assertIn("10k/200k", self.show(self.selection()))
        self.starts.pop(101)
        self.assertIn("teammate unavailable", self.show(self.selection()))

    def test_payload_cannot_claim_a_trusted_transport_origin(self):
        peer = self.peer(register=False)
        (self.root / "sessions" / "101.json").unlink()
        peer["origin_pid"] = 99999
        peer["statusline_origin_pid"] = 99999
        peer["statusline_process"]["origin_pid"] = 99999
        for origin in (None, 12345):
            self.show(peer, origin_pid=origin)
            self.assertIn("teammate unavailable", self.show(self.selection()))
        self.show(peer, origin_pid=99999)
        self.assertIn("Peer Alpha", self.show(self.selection()))

    def test_present_invalid_native_ledger_is_not_treated_as_missing(self):
        peer = self.peer(register=False)
        ledger = self.root / "sessions" / "101.json"
        for value in ("{invalid", "null", json.dumps({"pid": 101, "sessionId": "wrong", "procStart": "10100"})):
            ledger.write_text(value)
            self.show(peer)
            self.assertIn("teammate unavailable", self.show(self.selection()))

    def test_idle_ticks_for_48_sessions_do_not_write_sqlite_or_reread_argv(self):
        peers = [self.peer(pid=200+i, sid="idle-%d" % i, name="worker%d" % i, pane="%%%d" % i)
                 for i in range(48)]
        changes = self.cache.db.total_changes
        argv_reads = self.mocks[-1].call_count
        started = time.perf_counter()
        for tick in range(20):
            for peer in peers:
                native = peer["statusline_input"]
                native["cost"]["total_duration_ms"] = tick * 1000
                native["cost"]["total_api_duration_ms"] = tick * 700
                self.show(peer)
        self.assertEqual(self.cache.db.total_changes, changes)
        self.assertEqual(self.mocks[-1].call_count, argv_reads)
        self.idle_elapsed = time.perf_counter() - started


class NativeProcessParsingTest(unittest.TestCase):
    def test_real_procfs_ancestry_matches_parent_kernel_start(self):
        # This test workspace exposes outer procfs inside a nested PID
        # namespace, so use the pid domain actually exposed by /proc/self.
        with open("/proc/self/stat") as stream:
            raw = stream.read()
        own_pid = int(raw.split(" ", 1)[0])
        parent = int(raw[raw.rfind(")") + 2:].split()[1])
        start = statusline.process_start(parent)
        self.assertIsNotNone(start)
        self.assertTrue(statusline.process_descends_from(own_pid, parent, start))

    def test_proc_stat_allows_spaces_and_parentheses_in_comm(self):
        raw = "123 (a ) strange name)) S " + " ".join(str(i) for i in range(1, 25))
        with patch("builtins.open", return_value=io.StringIO(raw)):
            self.assertEqual(statusline.process_start(123), "19")

    def identity(self, *args):
        raw = ("claude\0" + "\0".join(args) + "\0").encode()
        with patch("builtins.open", return_value=io.BytesIO(raw)):
            return statusline.process_agent_identity(123)

    def test_exact_native_flags_support_both_forms_and_stop_at_separator(self):
        got = self.identity("--agent-id=alpha@team", "--agent-name", "alpha", "--team-name=team",
                            "--parent-session-id", "leader", "--", "--agent-id=wrong@team")
        self.assertEqual(got, {"agent_id": "alpha@team", "name": "alpha", "team": "team",
                               "parent_session_id": "leader"})
        self.assertEqual(self.identity("--", "--agent-id=alpha@team", "--team-name=team"), {})

    def test_inconsistent_or_traversing_identity_is_rejected(self):
        for args in (("--agent-id=alpha@team", "--team-name=other"),
                     ("--agent-id=alpha@../team", "--team-name=../team"),
                     ("--agent-id=alpha@team", "--team-name=team", "--agent-id=beta@team")):
            self.assertEqual(self.identity(*args), {})

    def test_native_ancestor_must_have_the_expected_kernel_start(self):
        chain = {90: ("900", 80), 80: ("800", 70), 70: ("700", 1)}
        with patch.object(statusline, "process_record", side_effect=lambda pid: chain.get(pid)):
            self.assertTrue(statusline.process_descends_from(90, 70, "700"))
            self.assertFalse(statusline.process_descends_from(90, 70, "reused"))
            self.assertFalse(statusline.process_descends_from(90, 60, "600"))
            self.assertFalse(statusline.process_descends_from(None, 70, "700"))

    def test_ancestry_walk_is_bounded_and_rejects_loops(self):
        with patch.object(statusline, "process_record", side_effect=lambda pid: (str(pid), pid - 1)) as read:
            self.assertFalse(statusline.process_descends_from(100, 1, "1"))
            self.assertEqual(read.call_count, 16)
        with patch.object(statusline, "process_record", return_value=("100", 100)):
            self.assertFalse(statusline.process_descends_from(100, 1, "1"))

    def test_socket_origin_uses_kernel_credentials_for_same_user(self):
        conn = Mock()
        conn.getsockopt.return_value = struct.pack("3i", 1234, os.getuid(), os.getgid())
        self.assertEqual(statusline.socket_peer_pid(conn), 1234)
        conn.getsockopt.return_value = struct.pack("3i", 1234, os.getuid() + 1, os.getgid())
        self.assertIsNone(statusline.socket_peer_pid(conn))


if __name__ == "__main__":
    unittest.main()
