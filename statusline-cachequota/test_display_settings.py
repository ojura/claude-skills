"""One shared renderer must respect each invoking terminal's display settings."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "display_statusline", Path(__file__).with_name("statusline-render.py"))
statusline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statusline)


class DisplaySettingsTest(unittest.TestCase):
    def setUp(self):
        statusline._display_profile.cache_clear()
        self.tmp = tempfile.TemporaryDirectory(prefix="statusline-display-")
        self.root = Path(self.tmp.name)
        self.transcript = self.root / "viewer.jsonl"
        self.transcript.write_text("")
        # Simulate a daemon started by an installer with an unrelated style.
        self.patches = [
            patch.object(statusline, "CLAUDE_DIR", str(self.root)),
            patch.multiple(statusline, **statusline._display_values({
                "NO_COLOR": "1", "CLAUDE_STATUSLINE_THEME": "light",
                "CLAUDE_STATUSLINE_COMPACT": "1"})),
            patch.dict(statusline.os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
            patch.object(statusline.time, "time", return_value=1792848000),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.clock = self.mocks[-1]
        self.cache = statusline.Cache(False)
        self.cache.values = Mock(return_value=None)
        self.cache.register_frame = Mock()
        self.memo = {}
        self.native = {
            "session_id": "viewer", "transcript_path": str(self.transcript),
            "model": {"id": "viewer-model", "display_name": "Viewer"},
            "context_window": {"total_input_tokens": 42000,
                               "context_window_size": 100000, "used_percentage": 42},
            "rate_limits": {
                "five_hour": {"used_percentage": 31, "resets_at": 1792851600},
                "seven_day": {"used_percentage": 64, "resets_at": 1793452800},
            },
        }

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def envelope(self, display):
        return {"statusline_input": self.native, "statusline_display": display}

    def show(self, display):
        return statusline.serve(self.envelope(display), self.cache, self.memo)

    def test_truecolor_client_recovers_original_dark_colors(self):
        line = self.show({"COLORTERM": "truecolor", "TERM": "xterm-256color"})
        self.assertIn("\033[38;2;", line)
        self.assertIn(";48;2;76;76;76m", line)
        self.assertIn("\033[38;2;136;136;136m", line)
        quota_rgb = ";".join(str(v) for v in statusline.ramp_rgb(31))
        self.assertIn("\033[38;2;" + quota_rgb + "m", line)
        self.assertIn("viewer", line)
        self.assertIn("(1:00:00)", line)

    def test_explicit_empty_does_not_inherit_daemon_style(self):
        inherited = statusline.serve(self.native, self.cache, self.memo)
        self.assertNotIn("\033[", inherited)
        self.assertNotIn("viewer", inherited)
        explicit = self.show({})
        self.assertIn("\033[2m", explicit)
        self.assertNotIn("\033[38;2;", explicit)
        self.assertIn("viewer", explicit)
        self.assertIn("(1:00:00)", explicit)

    def test_absent_display_preserves_defaults_after_explicit_request(self):
        inherited = statusline.serve(self.native, self.cache, self.memo)
        self.show({"COLORTERM": "truecolor"})
        self.assertEqual(statusline.serve(self.native, self.cache, self.memo), inherited)
        self.assertEqual(statusline.render(self.native, self.cache), inherited)
        self.assertFalse(statusline.COLOR_ENABLED)
        self.assertFalse(statusline.TRUECOLOR)
        self.assertTrue(statusline.COMPACT)
        self.assertEqual(statusline.THEME, "light")

    def test_no_color_and_explicit_color_modes_keep_existing_precedence(self):
        for env, colored in (
            ({"COLORTERM": "truecolor", "NO_COLOR": ""}, False),
            ({"CLAUDE_STATUSLINE_COLOR": "always", "NO_COLOR": ""}, False),
            ({"COLORTERM": "truecolor", "CLAUDE_STATUSLINE_COLOR": "never"}, False),
            ({"CLAUDE_STATUSLINE_COLOR": "always", "TERM": "dumb"}, True),
            ({"COLORTERM": "24BIT"}, True),
            ({"TERM": "xterm-direct"}, True),
        ):
            with self.subTest(env=env):
                line = self.show(env)
                self.assertEqual("\033[38;2;" in line, colored)
                if not colored:
                    self.assertNotIn("\033[", line)

    def test_light_palette_and_all_rgb_overrides_are_per_request(self):
        for light in ({"CLAUDE_STATUSLINE_THEME": "light"}, {"COLORFGBG": "0;15"}):
            line = self.show({"COLORTERM": "truecolor", **light})
            self.assertIn(";48;2;225;225;225m", line)
            self.assertIn("\033[38;2;90;90;90m", line)
        self.cache.values.return_value = (10, "5m", 80, 0, 0, False, 42000, "viewer-model")
        line = self.show({
            "COLORTERM": "truecolor", "CLAUDE_STATUSLINE_ALARM_RGB": "1,2,3",
            "CLAUDE_STATUSLINE_MUTED_RGB": "#040506",
            "CLAUDE_STATUSLINE_BAR_DIM_RGB": "7;8;9",
            "CLAUDE_STATUSLINE_BAR_ON_FILL_RGB": "#0a0b0c",
            "CLAUDE_STATUSLINE_BAR_ON_DIM_RGB": "13,14,15",
        })
        self.assertIn("\033[38;2;1;2;3m5m", line)
        self.assertIn("\033[38;2;4;5;6m", line)
        self.assertIn(";48;2;7;8;9m", line)
        self.assertIn("\033[38;2;10;11;12;48;2;", line)
        self.assertIn("\033[38;2;13;14;15;48;2;", line)
        restored = self.show({"COLORTERM": "truecolor"})
        self.assertIn(";48;2;76;76;76m", restored)
        self.assertNotIn(";48;2;7;8;9m", restored)

    def test_shared_memo_retains_each_display_presentation(self):
        color = {"COLORTERM": "truecolor"}
        plain = {"COLORTERM": "truecolor", "NO_COLOR": ""}
        with patch.object(statusline, "render_core", wraps=statusline.render_core) as core:
            colored = self.show(color)
            self.assertEqual(self.show(color), colored)
            self.assertEqual(core.call_count, 1)
            uncolored = self.show(plain)
            self.assertNotIn("\033[", uncolored)
            self.assertEqual(self.show(plain), uncolored)
            self.assertEqual(core.call_count, 2)
            self.assertEqual(self.show(color), colored)
            self.assertEqual(core.call_count, 2)
            compact = self.show({**color, "CLAUDE_STATUSLINE_COMPACT": "1"})
            self.assertNotIn("viewer", compact)
            self.assertNotIn("(1:00:00)", compact)
            self.assertEqual(core.call_count, 3)

    def test_memo_hit_countdown_keeps_invoking_clients_style(self):
        style = {"COLORTERM": "truecolor", "CLAUDE_STATUSLINE_MUTED_RGB": "1,2,3"}
        with patch.object(statusline, "render_core", wraps=statusline.render_core) as core:
            self.assertIn("(1:00:00)", self.show(style))
            self.clock.return_value += 61
            line = self.show(style)
            self.assertEqual(core.call_count, 1)
            self.assertIn("(0:58:59)\033[0m", line)
            self.assertIn("\033[38;2;1;2;3m→", line)
            self.assertNotIn("\033[2m→", line)

    def test_48_clients_reuse_profiles_and_rendered_memos_across_idle_cycles(self):
        profiles = [
            {"COLORTERM": "truecolor", "TERM": "xterm-256color"},
            {"NO_COLOR": ""},
            {"COLORTERM": "truecolor", "CLAUDE_STATUSLINE_THEME": "light",
             "CLAUDE_STATUSLINE_MUTED_RGB": "1,2,3"},
            {},
        ]
        requests = []
        for index in range(48):
            path = self.root / ("client-%d.jsonl" % index)
            path.write_text("")
            native = dict(self.native, session_id="client-%d" % index,
                          transcript_path=str(path), cost={"total_cost_usd": 2.5})
            requests.append({"statusline_input": native,
                             "statusline_display": profiles[index % len(profiles)]})
        with patch.object(statusline, "_display_values", wraps=statusline._display_values) as parse, \
                patch.object(statusline, "render_core", wraps=statusline.render_core) as core:
            lines = [statusline.serve(request, self.cache, self.memo) for request in requests]
            self.assertEqual(parse.call_count, len(profiles))
            self.assertEqual(core.call_count, len(requests))
            for cycle in range(20):
                # Rotate request order and update an undisplayed duration as
                # independently refreshing Claude clients do on idle ticks.
                for index in range(48):
                    client = (index + cycle) % len(requests)
                    request = requests[client]
                    request["statusline_input"]["cost"]["total_duration_ms"] = cycle * 1000
                    self.assertEqual(statusline.serve(request, self.cache, self.memo), lines[client])
            self.assertEqual(parse.call_count, len(profiles))
            self.assertEqual(core.call_count, len(requests))
            info = statusline._display_profile.cache_info()
            self.assertEqual(info.currsize, len(profiles))
            self.assertEqual(info.misses, len(profiles))
            self.assertEqual(info.hits, 48 * 21 - len(profiles))

    def test_profile_cache_is_bounded_and_retains_recently_used_profiles(self):
        profiles = [{"COLORTERM": "truecolor", "CLAUDE_STATUSLINE_MUTED_RGB": "%d,1,2" % value}
                    for value in range(129)]
        with patch.object(statusline, "_display_values", wraps=statusline._display_values) as parse:
            lines = [self.show(profile) for profile in profiles[:128]]
            self.assertEqual(parse.call_count, 128)
            self.assertEqual(self.show(profiles[0]), lines[0])  # refresh oldest
            self.show(profiles[128])  # evicts profile 1, not refreshed profile 0
            self.assertEqual(statusline._display_profile.cache_info().currsize, 128)
            self.assertEqual(parse.call_count, 129)
            self.assertEqual(self.show(profiles[0]), lines[0])
            self.assertEqual(parse.call_count, 129)
            self.assertEqual(self.show(profiles[1]), lines[1])
            self.assertEqual(parse.call_count, 130)
            self.assertEqual(statusline._display_profile.cache_info().currsize, 128)

    def test_selected_peer_uses_viewers_display_settings(self):
        peer_path = self.root / "peer.jsonl"
        peer_path.write_text("")
        peer = {"session_id": "peer", "transcript_path": str(peer_path),
                "model": {"id": "peer-model", "display_name": "Peer"},
                "context_window": {"context_window_size": 200000,
                                   "current_usage": {"input_tokens": 10000}}}
        self.cache.peer_frame = Mock(return_value=peer)
        # The peer previously rendered itself without colors in its own pane.
        statusline.serve({"statusline_input": peer, "statusline_display": {"NO_COLOR": "1"}},
                         self.cache, self.memo)
        activation = "v2_" + "a" * 32
        request = self.envelope({"COLORTERM": "truecolor"})
        request.update(statusline_activation=activation, statusline_view={
            "version": 2, "activation": activation, "session_id": "viewer",
            "agent_id": "alpha@team", "teammate_id": "alpha@team", "name": "Alpha"})
        colored = statusline.serve(request, self.cache, self.memo)
        self.assertIn("agent Alpha", colored)
        self.assertIn("Peer", colored)
        self.assertIn("\033[38;2;", colored)
        request["statusline_display"] = {"NO_COLOR": ""}
        plain = statusline.serve(request, self.cache, self.memo)
        self.assertIn("agent Alpha", plain)
        self.assertIn("10k/200k", plain)
        self.assertNotIn("\033[", plain)

    def test_malformed_display_is_bounded_and_cannot_leak_daemon_settings(self):
        expected = self.show({"COLORTERM": "truecolor"})
        for invalid_no_color in (False, None, 3, [], {}, "x" * 257, "Ž" * 129):
            with self.subTest(value=invalid_no_color):
                self.assertEqual(self.show({"COLORTERM": "truecolor", "NO_COLOR": invalid_no_color,
                                            "UNKNOWN_STYLE": "never",
                                            "CLAUDE_STATUSLINE_MUTED_RGB": {"bad": "type"}}), expected)
        self.assertNotIn("\033[", self.show({"COLORTERM": "truecolor", "NO_COLOR": "Ž" * 128}))
        empty = self.show({})
        for invalid in (None, [], "truecolor", 123):
            self.assertEqual(self.show(invalid), empty)

    def test_direct_fallback_and_shared_server_match(self):
        for style in ({"COLORTERM": "truecolor"}, {"NO_COLOR": ""}, {},
                      {"COLORTERM": "truecolor", "CLAUDE_STATUSLINE_COMPACT": "1"}):
            with self.subTest(style=style):
                self.assertEqual(statusline.render(self.envelope(style), self.cache), self.show(style))

    def test_display_defaults_are_restored_after_exception(self):
        before = {name: getattr(statusline, name) for name in statusline._STARTUP_DISPLAY}
        with patch.object(statusline, "selected_input", side_effect=RuntimeError("fixture")):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                self.show({"COLORTERM": "truecolor"})
        self.assertEqual({name: getattr(statusline, name) for name in statusline._STARTUP_DISPLAY}, before)


if __name__ == "__main__":
    unittest.main()
