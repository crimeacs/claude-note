"""Read-only fallback contracts against disposable real SQLite/FTS5 databases.

No user's index, SDK, subprocess, extension or model is accessed by these tests.
"""

import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_note import recall_lexical


class LexicalBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-note-lexical-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        (self.cache / "qmd").mkdir(parents=True)
        self.path = self.cache / "qmd/index.sqlite"
        self.environment = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        removed = mock.patch.dict(os.environ)
        removed.start()
        self.addCleanup(removed.stop)
        os.environ.pop("INDEX_PATH", None)
        self.create_database(self.path)

    def create_database(self, path, *, fts_options="tokenize='porter unicode61'"):
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE documents(id INTEGER PRIMARY KEY, collection TEXT NOT NULL,
              path TEXT NOT NULL,title TEXT NOT NULL,hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT NOT NULL);
            CREATE INDEX idx_documents_collection ON documents(collection,active);
            CREATE INDEX idx_documents_hash ON documents(hash);
        """)
        try:
            db.execute(f"CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body,{fts_options})")
        except sqlite3.OperationalError as error:
            db.close()
            self.skipTest(f"SQLite FTS5 unavailable: {error}")
        db.close()

    def add(self, collection="notes", path="book.md", title="Design book", body="design everyday feedback",
            active=1, *, database=None, fts_body=None):
        db = sqlite3.connect(database or self.path)
        revision = hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO content VALUES(?,?)", (revision, body))
        cursor = db.execute("INSERT INTO documents(collection,path,title,hash,active) VALUES(?,?,?,?,?)",
                            (collection, path, title, revision, active))
        db.execute("INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)",
                   (cursor.lastrowid, f"qmd://{collection}/{path}", title, body if fts_body is None else fts_body))
        db.commit()
        db.close()
        return revision

    def search(self, rungs=None, collections=None, memories=None, index="", deadline=None, limit=10):
        return recall_lexical.search_batch(rungs or [(7, "design everyday")], collections or [], memories or [],
                                          index, time.monotonic() + 2 if deadline is None else deadline, limit)

    def test_batch_uses_one_connection_and_keeps_scheduled_rung_indices_and_memory_scopes(self):
        self.add()
        self.add("agentmem", "lesson.md")
        connect = sqlite3.connect
        opened = []
        def record(*args, **kwargs):
            opened.append((args, kwargs))
            return connect(*args, **kwargs)
        with mock.patch.object(recall_lexical.sqlite3, "connect", side_effect=record):
            result = self.search([(8, "design everyday"), (2, "missing zebraplanet")], memories=["agentmem", "agentmem"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["reason"], "")
        self.assertEqual([(s, m, i) for s, m, i, _ in result["calls"]],
                         [(None, False, 8), (None, False, 2), ("agentmem", True, 8), ("agentmem", True, 2)])
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0][1]["uri"])
        self.assertTrue(opened[0][0][0].endswith("?mode=ro"))
        self.assertEqual(result["calls"][1][3], [])
        self.assertEqual(result["calls"][2][3][0]["file"], "qmd://agentmem/lesson.md")

    def test_scope_filter_precedes_limit_and_inactive_documents_are_excluded(self):
        for number in range(30):
            self.add("large", f"{number}.md", "design everyday", "design everyday")
        self.add("small", "target.md", "Target", "padding " * 100 + "design everyday")
        self.add("small", "inactive.md", "design everyday", "design everyday", active=0)
        result = self.search(collections=["small"], limit=1)
        self.assertEqual(result["calls"][0][:3], ("small", False, 7))
        self.assertEqual([row["file"] for row in result["calls"][0][3]], ["qmd://small/target.md"])

    def test_explicit_collections_union_memory_scopes_without_global_widening(self):
        self.add("notes", "notes.md")
        self.add("agentmem", "memory.md")
        self.add("other", "other.md")
        result = self.search(collections=["notes", "notes"], memories=["agentmem"])
        self.assertEqual([(scope, memory) for scope, memory, _, _ in result["calls"]],
                         [("notes", False), ("agentmem", True)])
        self.assertEqual({row["file"] for *_, rows in result["calls"] for row in rows},
                         {"qmd://notes/notes.md", "qmd://agentmem/memory.md"})

    def test_rows_have_metadata_but_no_unverified_body_prefix_evidence(self):
        revision = self.add(body="---\ntype: gotcha\n---\n" + "design everyday " * 2000)
        row = self.search()["calls"][0][3][0]
        self.assertEqual(set(row), {"file", "title", "revision", "revision_kind", "score"})
        self.assertEqual(row["revision"], revision)
        self.assertEqual(row["revision_kind"], "indexed_body_sha256")
        self.assertGreater(row["score"], 0)
        self.assertLessEqual(row["score"], 1)

    def test_materialized_match_is_not_repeated_for_active_document_rowids(self):
        self.add()
        connect = sqlite3.connect
        statements = []
        def record(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with mock.patch.object(recall_lexical.sqlite3, "connect", side_effect=record):
            self.assertTrue(self.search()["complete"])
        queries = [sql for sql in statements if "documents_fts MATCH" in sql]
        self.assertEqual(len(queries), 1)
        self.assertIn("fts_matches AS MATERIALIZED", queries[0])
        self.assertEqual(queries[0].count("documents_fts MATCH"), 1)
        self.assertNotIn("rowid IN", queries[0])
        self.assertNotIn("c.doc", queries[0])

    def test_literal_fts_does_not_interpret_operator_or_quote_input(self):
        self.add(path="literal.md", body="alpha or beta")
        self.add(path="operator.md", body="alpha beta")
        result = self.search([(0, '"alpha" OR beta')])
        self.assertTrue(result["complete"])
        self.assertEqual([row["file"] for row in result["calls"][0][3]], ["qmd://notes/literal.md"])
        self.assertEqual(recall_lexical.literal_fts('alpha) OR "beta"'), '"alpha"* AND "OR"* AND "beta"*')

    def test_unicode_and_cjk_use_pinned_normalized_fts_convention(self):
        self.add(path="cjk.md", title="设计", body="设计日常反馈", fts_body="设 计 日 常 反 馈")
        self.add(path="unicode.md", title="Оплата", body="повторная оплата")
        result = self.search([(0, "设计"), (1, "повторная оплата")])
        self.assertEqual(result["calls"][0][3][0]["file"], "qmd://notes/cjk.md")
        self.assertEqual(result["calls"][1][3][0]["file"], "qmd://notes/unicode.md")

    def test_named_index_reads_only_named_database_and_never_falls_back_to_default(self):
        self.add()
        self.assertIsNone(self.search(index="missing"))
        named = self.cache / "qmd/customer.sqlite"
        self.create_database(named)
        self.add(path="named.md", database=named)
        self.assertEqual(self.search(index="customer")["calls"][0][3][0]["file"], "qmd://notes/named.md")
        self.assertFalse((self.cache / "qmd/missing.sqlite").exists())

    def test_explicit_index_path_override_matches_sdk_even_for_named_index(self):
        override = self.root / "explicit # index.sqlite"
        self.create_database(override)
        self.add(path="override.md", database=override)
        with mock.patch.dict(os.environ, {"INDEX_PATH": str(override)}):
            self.assertEqual(self.search(index="customer")["calls"][0][3][0]["file"], "qmd://notes/override.md")

    def test_invalid_index_names_are_not_interpreted_as_paths(self):
        for name in ("../index", "..", "index/other", "/tmp/index", "a\\b", "bad\0name"):
            with self.subTest(name=name):
                self.assertIsNone(self.search(index=name))

    def test_missing_index_does_not_create_cache_or_database(self):
        missing = self.root / "does-not-exist"
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(missing)}):
            self.assertIsNone(self.search())
        self.assertFalse(missing.exists())

    def test_schema_guards_reject_contentless_fts_and_wrong_document_layout(self):
        contentless = self.cache / "qmd/contentless.sqlite"
        self.create_database(contentless, fts_options="content='',tokenize='porter unicode61'")
        self.assertIsNone(self.search(index="contentless"))
        wrong = self.cache / "qmd/wrong.sqlite"
        db = sqlite3.connect(wrong)
        db.execute("CREATE TABLE documents(id TEXT PRIMARY KEY)")
        db.close()
        self.assertIsNone(self.search(index="wrong"))

    def test_read_only_search_leaves_database_bytes_and_mtime_unchanged(self):
        self.add()
        before, modified = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertTrue(self.search()["complete"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, modified)

    def test_expired_deadline_never_opens_index(self):
        with mock.patch.object(recall_lexical.sqlite3, "connect") as connect:
            self.assertIsNone(self.search(deadline=time.monotonic() - 1))
        connect.assert_not_called()

    def test_progress_handler_interrupts_real_fts_and_preserves_only_completed_calls(self):
        db = sqlite3.connect(self.path)
        body = "common design everyday"
        revision = hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT INTO content VALUES(?,?)", (revision, body))
        db.executemany("INSERT INTO documents VALUES(?,?,?,?,?,?)",
                       ((i, "notes", f"{i}.md", "Book", revision, 1) for i in range(1, 10001)))
        db.executemany("INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)",
                       ((i, f"qmd://notes/{i}.md", "Book", body) for i in range(1, 10001)))
        db.commit()
        db.close()
        clock_calls = []
        def ticking_clock():
            clock_calls.append(True)
            return len(clock_calls) * .001
        with mock.patch.object(recall_lexical.time, "monotonic", side_effect=ticking_clock):
            result = self.search([(1, "nonexistentzebraplanet"), (2, "common")], deadline=.1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "deadline")
        self.assertEqual(result["calls"], [(None, False, 1, [])])
        self.assertGreaterEqual(len(clock_calls), 100)

    def test_locked_database_busy_wait_is_bounded_by_remaining_deadline(self):
        self.add()
        writer = sqlite3.connect(self.path)
        writer.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            self.assertIsNone(self.search(deadline=started + .035))
            self.assertLess(time.monotonic() - started, .13)
        finally:
            writer.rollback()
            writer.close()


if __name__ == "__main__":
    unittest.main()
