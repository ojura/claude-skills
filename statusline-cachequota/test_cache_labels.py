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

    def assert_no_cause(self, text):
        for label in (" hot", "evicted", "expired", "(cold)", "bust "):
            self.assertNotIn(label, text)

    def test_five_minute_cache_after_six_minute_gap_has_factual_age(self):
        event = self.FIRST + 360
        data = self.transcript("5m", event)
        text = self.display(data, event + 720)
        self.assertIn("cache miss 12m ago", text)
        self.assertIn("TTL elapsed", text)
        self.assertIn("→ ~", text)
        self.assert_no_cause(text)

    def test_one_hour_cache_after_seventy_minute_gap_is_not_called_evicted(self):
        event = self.FIRST + 4200
        data = self.transcript("1h", event)
        text = self.display(data, event + 60)
        self.assertIn("cache miss 1m ago", text)
        stamp = statusline.time.strftime("%H:%M", statusline.time.localtime(event + 3600))
        self.assertIn("→ ~" + stamp, text)
        self.assertNotIn("TTL elapsed", text)
        self.assert_no_cause(text)

    def test_missing_or_backwards_timestamps_do_not_invent_causes(self):
        for name, event in (("missing", None), ("backwards", self.FIRST - 60)):
            with self.subTest(name=name):
                data = self.transcript("5m", event, name)
                text = self.display(data, self.FIRST + 600)
                self.assert_no_cause(text)
                if event is None:
                    self.assertNotIn("cache miss", text)
                else:
                    # The recorded event time still gives an age; an earlier
                    # timestamp provides no basis for a cache-loss diagnosis.
                    self.assertIn("cache miss 11m ago", text)

    def test_unknown_ttl_has_no_guessed_expiry(self):
        event = self.FIRST + 60
        data = self.transcript(None, event)
        text = self.display(data, event + 720)
        self.assertIn("cache miss 12m ago", text)
        self.assertNotIn("→", text)
        self.assertNotIn("TTL elapsed", text)
        self.assert_no_cause(text)
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
        self.assertIn("(5m TTL elapsed)", text)
        self.assert_no_cause(text)

    def test_event_age_updates_on_an_unchanged_transcript_refresh(self):
        event = self.FIRST + 60
        data = self.transcript("1h", event)
        memo = {}
        first = self.display(data, event + 60, memo)
        later = self.display(data, event + 180, memo)
        self.assertIn("cache miss 1m ago", first)
        self.assertIn("cache miss 3m ago", later)
        self.assertNotIn("cache miss 1m ago", later)


if __name__ == "__main__":
    unittest.main()
