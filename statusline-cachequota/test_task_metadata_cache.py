"""Unchanged native task feeds and selected-task reads should reuse metadata."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "task_cache_statusline", Path(__file__).with_name("statusline-render.py"))
statusline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(statusline)


class TaskMetadataCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="statusline-task-cache-")
        self.root = Path(self.tmp.name)
        self.path = str(self.root / "main.jsonl")
        self.db_patch = patch.object(statusline, "DB_PATH", str(self.root / "cache.db"))
        self.db_patch.start()
        self.caches = []
        self.cache = self.new_cache()

    def tearDown(self):
        for cache in self.caches:
            if cache.db is not None:
                cache.db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    def new_cache(self):
        cache = statusline.Cache(False)
        self.caches.append(cache)
        return cache

    def task(self, agent="a1", **fields):
        return {"id": agent, "model": "model-a", "effort": "medium",
                "contextWindowSize": 200000, "name": "Alpha", **fields}

    def feed(self, *tasks, session="main", path=None):
        return {"session_id": session, "transcript_path": path or self.path,
                "tasks": list(tasks)}

    def info(self, agent="a1", session="main", path=None, cache=None):
        return (cache or self.cache).task_info(session, path or self.path, agent)

    def trace(self, cache=None):
        statements = []
        (cache or self.cache).db.set_trace_callback(statements.append)
        return statements

    def test_48_sessions_20_idle_task_feeds_do_not_write_or_read_rows(self):
        feeds = [self.feed(*(self.task("agent-%d" % agent) for agent in range(4)),
                           session="session-%d" % session,
                           path=str(self.root / ("session-%d.jsonl" % session)))
                 for session in range(48)]
        for feed in feeds:
            self.cache.ingest_tasks(feed)
        initial_changes = self.cache.db.total_changes
        self.assertEqual(initial_changes, 192)
        statements = self.trace()
        with patch.object(statusline.json, "loads", wraps=statusline.json.loads) as decode, \
                patch.object(statusline.json, "dumps", wraps=statusline.json.dumps) as encode:
            for tick in range(20):
                for feed in feeds:
                    for task in feed["tasks"]:
                        task.update(durationMs=tick * 1000, tokenCount=tick * 50, status="running")
                    self.cache.ingest_tasks(feed)
            self.assertEqual(decode.call_count, 0)
            self.assertEqual(encode.call_count, 0)
        self.assertEqual(self.cache.db.total_changes, initial_changes)
        self.assertFalse(any(s.startswith(("INSERT", "UPDATE", "DELETE", "SELECT", "BEGIN", "COMMIT"))
                             for s in statements))
        self.assertEqual(statements.count("PRAGMA data_version"), 960)

    def test_repeated_task_info_uses_decoded_metadata(self):
        self.cache.ingest_tasks(self.feed(self.task()))
        statements = self.trace()
        with patch.object(statusline.json, "loads", wraps=statusline.json.loads) as decode:
            for _ in range(100):
                self.assertEqual(self.info()["contextWindowSize"], 200000)
            self.assertEqual(decode.call_count, 0)
        self.assertFalse(any(s.startswith("SELECT") for s in statements))
        # Returning a dict must not let the caller replace cached fields.
        self.info()["model"] = "caller-edit"
        self.assertEqual(self.info()["model"], "model-a")

    def test_real_changes_write_only_the_changed_agent(self):
        alpha, beta = self.task(), self.task("b1", name="Beta")
        self.cache.ingest_tasks(self.feed(alpha, beta))
        changes = self.cache.db.total_changes
        for field, value in (("model", "model-b"), ("effort", "high"),
                             ("contextWindowSize", 1000000), ("name", "Renamed")):
            statements = self.trace()
            alpha[field] = value
            self.cache.ingest_tasks(self.feed(alpha, beta))
            changes += 1
            self.assertEqual(self.cache.db.total_changes, changes)
            self.assertEqual(sum(s.startswith("INSERT") for s in statements), 1)
            self.assertEqual(statements.count("COMMIT"), 1)
            self.assertEqual(self.info()[field], value)
            self.assertEqual(self.info("b1")["name"], "Beta")

    def test_identity_is_session_transcript_and_agent(self):
        other_path = str(self.root / "other.jsonl")
        cases = [("main", self.path, "a1", "first"),
                 ("other", self.path, "a1", "other-session"),
                 ("main", other_path, "a1", "other-path"),
                 ("main", self.path, "b1", "other-agent")]
        for session, path, agent, model in cases:
            self.cache.ingest_tasks(self.feed(self.task(agent, model=model), session=session, path=path))
        self.assertEqual(self.cache.db.total_changes, len(cases))
        for session, path, agent, model in cases:
            self.assertEqual(self.info(agent, session, path)["model"], model)

    def test_missing_or_finished_rows_keep_older_selected_metadata(self):
        self.cache.ingest_tasks(self.feed(self.task(), self.task("b1", name="Beta")))
        changes = self.cache.db.total_changes
        self.cache.ingest_tasks(self.feed())
        self.cache.ingest_tasks(self.feed(self.task("b1", name="Beta", status="completed")))
        self.assertEqual(self.cache.db.total_changes, changes)
        self.assertEqual(self.info()["name"], "Alpha")
        self.assertEqual(self.info("b1")["name"], "Beta")

    def test_restart_reads_persisted_rows_without_rewriting_them(self):
        feed = self.feed(self.task(), self.task("b1", name="Beta"))
        self.cache.ingest_tasks(feed)
        self.cache.db.close()
        self.cache.db = None
        restarted = self.new_cache()
        restarted.db = statusline.open_db()
        statements = self.trace(restarted)
        restarted.ingest_tasks(feed)
        self.assertEqual(restarted.db.total_changes, 0)
        self.assertEqual(sum(s.startswith("SELECT payload") for s in statements), 2)
        self.assertNotIn("COMMIT", statements)
        statements.clear()
        with patch.object(statusline.json, "loads", wraps=statusline.json.loads) as decode:
            restarted.ingest_tasks(feed)
            self.assertEqual(self.info(cache=restarted)["model"], "model-a")
            self.assertEqual(self.info("b1", cache=restarted)["name"], "Beta")
            self.assertEqual(decode.call_count, 0)
        self.assertFalse(any(s.startswith("SELECT") for s in statements))

    def test_bounded_eviction_keeps_database_rows_available(self):
        with patch.object(statusline, "TASK_METADATA_CACHE_SIZE", 3):
            self.cache.ingest_tasks(self.feed(*(self.task("a%d" % i) for i in range(3))))
            self.info("a0")  # Retain this older selected task in the LRU.
            self.cache.ingest_tasks(self.feed(self.task("a3")))
            self.assertEqual(len(self.cache.task_metadata), 3)
            self.assertIn(("main", self.path, "a0"), self.cache.task_metadata)
            self.assertNotIn(("main", self.path, "a1"), self.cache.task_metadata)
            changes = self.cache.db.total_changes
            statements = self.trace()
            self.assertEqual(self.info("a1")["model"], "model-a")
            self.assertEqual(sum(s.startswith("SELECT payload") for s in statements), 1)
            self.assertEqual(len(self.cache.task_metadata), 3)
            self.assertEqual(self.cache.db.total_changes, changes)
            # Even after cache eviction, replaying this metadata does not write.
            self.cache.ingest_tasks(self.feed(self.task("a2")))
            self.assertEqual(self.cache.db.total_changes, changes)

    def test_other_connection_updates_invalidate_cached_reads_and_feeds(self):
        original = self.feed(self.task())
        self.cache.ingest_tasks(original)
        fallback = self.new_cache()
        fallback.ingest_tasks(self.feed(self.task(model="fallback-model", contextWindowSize=1000000)))
        self.assertEqual(self.info()["model"], "fallback-model")
        self.assertEqual(self.info()["contextWindowSize"], 1000000)
        # The original producer's next feed is authoritative again. It must
        # not be skipped because an older in-memory snapshot matched it.
        before = self.cache.db.total_changes
        self.cache.ingest_tasks(original)
        self.assertEqual(self.cache.db.total_changes, before + 1)
        self.assertEqual(self.info(cache=fallback)["model"], "model-a")
        fallback.ingest_tasks(self.feed(self.task(model="external-again")))
        before = self.cache.db.total_changes
        self.cache.ingest_tasks(original)  # No intervening task_info read.
        self.assertEqual(self.cache.db.total_changes, before + 1)
        self.assertEqual(self.info(cache=fallback)["model"], "model-a")

    def test_missing_read_is_replaced_by_later_feed(self):
        self.assertEqual(self.info(), {})
        self.cache.ingest_tasks(self.feed(self.task()))
        self.assertEqual(self.info()["name"], "Alpha")
        fallback = self.new_cache()
        self.assertEqual(self.info("b1"), {})
        fallback.ingest_tasks(self.feed(self.task("b1", name="Beta")))
        self.assertEqual(self.info("b1")["name"], "Beta")

    def test_failed_batch_rolls_back_before_unrelated_commit_and_retries_cleanly(self):
        self.cache.ingest_tasks(self.feed(self.task()))
        self.cache.db.execute(
            "CREATE TEMP TRIGGER fail_task_metadata BEFORE INSERT ON task_metadata "
            "WHEN NEW.agent_id='fail' BEGIN SELECT RAISE(ABORT, 'fixture task failure'); END")
        failed_feed = self.feed(self.task(model="changed-before-failure"), self.task("fail"))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture task failure"):
            self.cache.ingest_tasks(failed_feed)
        self.assertFalse(self.cache.db.in_transaction)
        self.assertEqual(len(self.cache.task_metadata), 0)
        # Another feature may commit on the same connection after the failed
        # feed. It must not accidentally publish the partially applied task.
        self.cache.db.execute("CREATE TABLE unrelated_refresh(value INT)")
        self.cache.db.execute("INSERT INTO unrelated_refresh VALUES(1)")
        self.cache.db.commit()
        row = self.cache.db.execute("SELECT payload FROM task_metadata WHERE agent_id='a1'").fetchone()
        self.assertEqual(statusline.json.loads(row[0])["model"], "model-a")
        self.assertEqual(self.info()["model"], "model-a")
        self.assertEqual(self.info("fail"), {})
        self.cache.db.execute("DROP TRIGGER fail_task_metadata")
        self.cache.ingest_tasks(failed_feed)
        self.assertEqual(self.info()["model"], "changed-before-failure")
        self.assertEqual(self.info("fail")["model"], "model-a")
        before = self.cache.db.total_changes
        self.cache.ingest_tasks(failed_feed)
        self.assertEqual(self.cache.db.total_changes, before)

    def test_duplicate_ids_keep_last_row_and_invalid_empty_feeds_do_not_open_database(self):
        for feed in (self.feed(), self.feed(None, 3, {"id": "../unsafe"}),
                     {"tasks": [self.task()]}):
            self.cache.ingest_tasks(feed)
        self.assertIsNone(self.cache.db)
        self.cache.ingest_tasks(self.feed(self.task(model="old"), self.task(model="last")))
        self.assertEqual(self.cache.db.total_changes, 1)
        self.assertEqual(self.info()["model"], "last")


if __name__ == "__main__":
    unittest.main()
