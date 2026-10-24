import importlib.util
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "statusline_cache_labels", Path(__file__).with_name("statusline-render.py"))
statusline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statusline)


class CacheEventLabelsTest(unittest.TestCase):
    FIRST = 1_792_836_000

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.folder = Path(folder)
        self.stack.enter_context(patch.object(statusline, "DB_PATH", folder + "/cache.db"))
        self.stack.enter_context(patch.object(statusline, "SIDECAR", folder + "/cache.state.json"))
        self.stack.enter_context(patch.object(statusline, "CLAUDE_DIR", folder))
        self.stack.enter_context(patch.object(statusline, "COLOR_ENABLED", False))
        self.stack.enter_context(patch.object(statusline, "TRUECOLOR", False))
        self.stack.enter_context(patch.object(statusline, "COMPACT", False))
        self.stack.enter_context(patch.dict(statusline.os.environ,
                                           {"CLAUDE_CODE_OAUTH_TOKEN": ""}))
        self.cache = statusline.Cache(persistent=False)
        self.addCleanup(lambda: self.cache.db.close() if self.cache.db else None)

    @staticmethod
    def response(uuid, parent, timestamp, ttl):
        usage = {"input_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 100_000}
        if ttl == "5m":
            usage["cache_creation"] = {"ephemeral_5m_input_tokens": 100_000}
        elif ttl == "1h":
            usage["cache_creation"] = {"ephemeral_1h_input_tokens": 100_000}
        elif ttl == "5m+1h":
            usage["cache_creation"] = {"ephemeral_5m_input_tokens": 50_000,
                                       "ephemeral_1h_input_tokens": 50_000}
        entry = {"uuid": uuid, "parentUuid": parent, "type": "assistant",
                 "message": {"id": uuid, "model": "test-model", "usage": usage}}
        if timestamp is not None:
            entry["timestamp"] = datetime.fromtimestamp(
                timestamp, timezone.utc).isoformat()
        return entry

    def transcript(self, ttl, second, name="session"):
        path = self.folder / (name + ".jsonl")
        rows = [self.response("first", None, self.FIRST, ttl),
                self.response("second", "first", second, ttl)]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return {"transcript_path": str(path), "model": {"id": "test-model"}}

    def display(self, data, now, memo=None):
        with patch.object(statusline.time, "time", return_value=now):
            return statusline.serve(data, self.cache, memo if memo is not None else {})

    def test_five_minute_cache_keeps_original_hot_band_and_separate_segment(self):
        event = self.FIRST + 360
        data = self.transcript("5m", event)
        text = self.display(data, event + 720)
        self.assertIn("recached 1.00x · bust 12m hot", text)
        self.assertNotIn("miss ", text)
        self.assertIn("(cold)", text)
        self.assertNotIn("TTL elapsed", text)
        self.assertIn("→ ~", text)
        self.assertNotIn("(past)", text)

    def test_one_hour_cache_keeps_original_evicted_band(self):
        event = self.FIRST + 4200
        data = self.transcript("1h", event)
        text = self.display(data, event + 60)
        self.assertIn("bust 1m evicted", text)
        stamp = statusline.time.strftime("%H:%M", statusline.time.localtime(event + 3600))
        self.assertIn("→ ~" + stamp, text)
        self.assertNotIn("(cold)", text)

    def test_missing_or_backwards_timestamps_do_not_invent_bands(self):
        for name, event in (("missing", None), ("backwards", self.FIRST - 60)):
            with self.subTest(name=name):
                data = self.transcript("5m", event, name)
                text = self.display(data, self.FIRST + 600)
                for marker in (" hot", "evicted", "expired"):
                    self.assertNotIn(marker, text)
                if event is None:
                    self.assertNotIn("bust ", text)
                else:
                    # The recorded event time still gives an age; an earlier
                    # timestamp provides no basis for a cache-loss diagnosis.
                    self.assertIn("bust 11m", text)

    def test_unknown_ttl_has_no_guessed_expiry(self):
        event = self.FIRST + 60
        data = self.transcript(None, event)
        text = self.display(data, event + 720)
        self.assertIn("bust 12m hot", text)
        self.assertNotIn("→", text)
        self.assertNotIn("(cold)", text)
        # A persisted memo from an older renderer may still contain a guessed
        # five-minute expiry. The visible result must reject that estimate too.
        old = (50, "?", 100, event + 300, event, 2, 100_000, "test-model")
        with patch.object(self.cache, "values", return_value=old):
            self.assertNotIn("→", self.display(data, event + 720))

    def test_mixed_ttl_marks_only_the_earliest_boundary(self):
        event = self.FIRST + 60
        data = self.transcript("5m+1h", event)
        text = self.display(data, event + 301)
        self.assertIn("cache 5m+1h", text)
        self.assertIn("(cold)", text)
        self.assertNotIn("TTL elapsed", text)
        self.assertIn("bust 5m hot", text)

    def test_event_age_updates_on_an_unchanged_transcript_refresh(self):
        event = self.FIRST + 60
        data = self.transcript("1h", event)
        memo = {}
        first = self.display(data, event + 60, memo)
        later = self.display(data, event + 180, memo)
        self.assertIn("bust 1m hot", first)
        self.assertIn("bust 3m hot", later)
        self.assertNotIn("bust 1m hot", later)

    def test_original_band_boundaries_and_unknown_previous_time(self):
        for gap, marker in ((599, "hot"), (600, "evicted"),
                            (7199, "evicted"), (7200, "expired")):
            with self.subTest(gap=gap):
                event = self.FIRST + gap
                data = self.transcript("1h", event, str(gap))
                self.assertIn("bust 12m " + marker, self.display(data, event + 720))
        path = self.folder / "unknown-gap.jsonl"
        rows = [self.response("first", None, None, "1h"),
                self.response("second", "first", self.FIRST, "1h")]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        text = self.display({"transcript_path": str(path)}, self.FIRST + 720)
        self.assertIn("bust 12m", text)
        for marker in (" hot", "evicted", "expired"):
            self.assertNotIn(marker, text)

    def test_original_marker_colours_and_plain_output(self):
        for gap, marker in ((60, "hot"), (600, "evicted"), (7200, "expired")):
            with self.subTest(marker=marker):
                event = self.FIRST + gap
                data = self.transcript("1h", event, "colour-" + marker)
                with patch.multiple(statusline, **statusline._display_values({
                        "COLORTERM": "truecolor", "CLAUDE_STATUSLINE_THEME": "dark"})):
                    styled = (statusline.paint("hot", fg=statusline.ALARM_RGB) if marker == "hot"
                              else statusline.muted("expired") if marker == "expired" else marker)
                    if marker != "evicted":
                        self.assertIn("\033[", styled)
                    self.assertIn("bust 12m " + styled, self.display(data, event + 720))
                plain = self.display(data, event + 720)
                self.assertIn("bust 12m " + marker, plain)
                self.assertNotIn("\033[", plain)

    def test_clean_response_preserves_the_previous_bust_marker(self):
        event = self.FIRST + 60
        data = self.transcript("1h", event)
        self.assertIn("bust 1m hot", self.display(data, event + 60))
        clean = self.response("third", "second", event + 30, "1h")
        clean["message"]["usage"] = {"input_tokens": 0, "cache_read_input_tokens": 100_000,
                                      "cache_creation_input_tokens": 0}
        with open(data["transcript_path"], "a") as stream:
            stream.write(json.dumps(clean) + "\n")
        self.assertIn("bust 3m hot", self.display(data, event + 180))

    def test_old_zero_band_state_replays_once_and_preserves_other_tables(self):
        event = self.FIRST + 60
        data = self.transcript("1h", event)
        self.display(data, event + 60)
        db = self.cache.db
        db.execute("UPDATE nodes SET busthot=0")
        db.execute("INSERT INTO task_metadata VALUES(?,?,?,?)", ("owner", "path", "worker", "{}"))
        db.execute("INSERT INTO peer_frames VALUES(?,?,?,?,?,?,?,?,?)",
                   ("peer", 123, "456", "team", "worker@team", "owner", "%7", "{}", 1))
        db.execute("PRAGMA user_version=0")
        db.commit()
        for entry in self.cache.mem.values():
            entry[6][5] = 0
        Path(statusline.SIDECAR).write_text(json.dumps({"version": 2, "entries": self.cache.mem}))
        db.close()
        self.cache.db = None
        self.cache = statusline.Cache(persistent=True)
        self.assertEqual(self.cache.mem, {})
        self.assertIn("bust 1m hot", self.display(data, event + 60))
        saved = json.loads(Path(statusline.SIDECAR).read_text())
        self.assertEqual(saved["version"], statusline.CACHE_MEMO_VERSION)
        rebuilt = statusline.open_db()
        try:
            self.assertEqual(rebuilt.execute("PRAGMA user_version").fetchone()[0],
                             statusline.CACHE_DB_VERSION)
            self.assertEqual(rebuilt.execute("SELECT busthot FROM nodes WHERE uuid='second'").fetchone()[0], 1)
            self.assertEqual(rebuilt.execute("SELECT COUNT(*) FROM task_metadata").fetchone()[0], 1)
            self.assertEqual(rebuilt.execute("SELECT COUNT(*) FROM peer_frames").fetchone()[0], 1)
            count = rebuilt.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        finally:
            rebuilt.close()
        reopened = statusline.open_db()
        try:
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], count)
        finally:
            reopened.close()

    def test_expiry_state_remains_visible_when_memo_crosses_boundary(self):
        event = self.FIRST + 60
        data = self.transcript("5m", event)
        memo = {}
        before = self.display(data, event + 299, memo)
        after = self.display(data, event + 300, memo)
        self.assertIn("(0:01)", before)
        self.assertNotIn("(cold)", before)
        self.assertIn("(cold)", after)
        self.assertNotIn("(0:01)", after)
        self.assertNotIn("TTL elapsed", after)

    def test_cold_warning_is_red_for_single_and_mixed_ttls(self):
        event = self.FIRST + 60
        with patch.multiple(statusline, **statusline._display_values({"COLORTERM": "truecolor"})):
            expected = statusline.paint("(cold)", fg=statusline.ALARM_RGB)
            self.assertIn("\033[", expected)
            for ttl, duration in (("5m", 300), ("1h", 3600), ("5m+1h", 300)):
                with self.subTest(ttl=ttl):
                    data = self.transcript(ttl, event, "cold-" + ttl)
                    self.assertIn(expected, self.display(data, event + duration))


if __name__ == "__main__":
    unittest.main()
