"""Fast read-only keyword fallback for the pinned QMD 2.8.3 index layout.

No QMD process, SDK, extension or model is loaded. ``search_batch`` accepts
scheduled ``(original_rung_index, query)`` pairs and a monotonic deadline. It
returns completed ``(scope, is_memory, rung_index, rows)`` calls, including empty
successful searches, plus explicit completion/reason fields. ``None`` means the
adapter was unavailable before any search completed; the caller may try its CLI.

Rows contain metadata only. A title or indexed body prefix is never substituted
for evidence: the recall client must resolve and verify the actual source.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path


_INDEX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_TERMS = re.compile(r"\w+", re.UNICODE)
_REQUIRED = {
    "documents": {"id": "INTEGER", "collection": "TEXT", "path": "TEXT", "title": "TEXT",
                  "hash": "TEXT", "active": "INTEGER"},
    "content": {"hash": "TEXT", "doc": "TEXT"},
}


def _db_path(index: str) -> Path | None:
    # QMD 2.8.3 store.js:getDefaultDbPath. Unlike the SDK's production opener,
    # this read-only adapter never creates the cache directory or database.
    if index and not _INDEX_NAME.fullmatch(index):
        return None
    override = os.environ.get("INDEX_PATH")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return Path(override).absolute() if override else (cache / "qmd" / f"{index or 'index'}.sqlite").absolute()


def _is_cjk(char: str) -> bool:
    return unicodedata.name(char, "").startswith(("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "IDEOGRAPHIC"))


def literal_fts(query: str) -> str:
    """Literal prefix terms, with QMD's normalized CJK phrase convention."""
    out = []
    for term in _TERMS.findall(query)[:32]:
        if any(_is_cjk(char) for char in term):
            spaced, run = [], []
            for char in term:
                if _is_cjk(char):
                    run.append(char)
                else:
                    if run:
                        spaced.append(" " + " ".join(run) + " ")
                        run = []
                    spaced.append(char)
            if run:
                spaced.append(" " + " ".join(run) + " ")
            out.append('"' + "".join(spaced).strip() + '"')
        else:
            out.append('"' + term + '"*')
    return " AND ".join(out)


def _schema_supported(db: sqlite3.Connection) -> bool:
    # Guard structure rather than importing an installed package whose version
    # does not establish which database this connection actually opened.
    definitions = {row[0]: (row[1], row[2] or "") for row in db.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE name IN ('documents','content','documents_fts')")}
    for table, required in _REQUIRED.items():
        kind, sql = definitions.get(table, ("", ""))
        if kind != "table" or re.search(r"\bUSING\b", sql, re.IGNORECASE):
            return False
        columns = {row[1]: row for row in db.execute(f"PRAGMA table_info({table})")}
        if any(name not in columns or columns[name][2].upper() != column_type
               for name, column_type in required.items()):
            return False
        primary = "id" if table == "documents" else "hash"
        if columns[primary][5] != 1:
            return False
    kind, sql = definitions.get("documents_fts", ("", ""))
    if (kind != "table" or not re.search(r"\bUSING\s+fts5\s*\(", sql, re.IGNORECASE)
            or re.search(r"\bcontent\s*=", sql, re.IGNORECASE)
            or not re.search(r"\btokenize\s*=\s*['\"]porter\s+unicode61['\"]", sql, re.IGNORECASE)):
        return False
    return [row[1] for row in db.execute("PRAGMA table_info(documents_fts)")] == ["filepath", "title", "body"]


def _rows(db: sqlite3.Connection, query: str, scope: str | None, limit: int) -> list[dict]:
    match = literal_fts(query)
    if not match:
        return []
    scope_sql = "AND d.collection=?" if scope is not None else ""
    # Materialize MATCH once. A rowid IN(all active documents) predicate causes
    # FTS5 to repeat its search for every active document. Scope still precedes
    # LIMIT, so a small collection cannot be starved by global top-k results.
    sql = f"""WITH fts_matches AS MATERIALIZED (
        SELECT rowid,bm25(documents_fts,1.5,4.0,1.0) AS rank FROM documents_fts
        WHERE documents_fts MATCH ?
      ), matches AS MATERIALIZED (
        SELECT fm.rowid,fm.rank FROM fts_matches fm JOIN documents d ON d.id=fm.rowid
        WHERE d.active=1 {scope_sql}
        ORDER BY fm.rank ASC,fm.rowid ASC LIMIT ?
      ) SELECT d.collection,d.path,d.title,d.hash,matches.rank FROM matches
        JOIN documents d ON d.id=matches.rowid JOIN content c ON c.hash=d.hash
        ORDER BY matches.rank ASC,d.collection ASC,d.path ASC"""
    parameters = [match, *([scope] if scope is not None else []), limit]
    rows = []
    for collection, path, title, revision, rank in db.execute(sql, parameters):
        if not all(isinstance(value, str) for value in (collection, path, title, revision)):
            continue
        uri = f"qmd://{collection}/{path}"
        if len(uri.encode()) > 4096 or not isinstance(rank, (int, float)) or not math.isfinite(rank):
            continue
        rows.append({"file": uri, "title": title[:160], "revision": revision,
                     "revision_kind": "indexed_body_sha256", "score": abs(rank) / (1 + abs(rank))})
    return rows


def search_batch(rungs: list[tuple[int, str]], collections: list[str], memory_collections: list[str],
                 index: str, deadline: float, limit: int = 10) -> dict | None:
    """Search all scheduled rungs/scopes using one read-only SQLite connection.

    ``complete=False`` retains completed calls on expiry/failure; it is never a
    conclusive miss. The caller still owns its outer absolute deadline, including
    filesystem opening and source verification. No database path is guessed after
    an unsupported/missing index, and no directory, schema or index is written.
    """
    calls: list[tuple[str | None, bool, int, list[dict]]] = []
    db, available = None, False
    try:
        if (time.monotonic() >= deadline or sqlite3.sqlite_version_info < (3, 35, 0)
                or len(rungs) > 32 or len(collections) + len(memory_collections) > 80
                or any(not isinstance(i, int) or i < 0 or not isinstance(q, str) or len(q) > 2000 for i, q in rungs)
                or any(not isinstance(c, str) or not c or len(c) > 200 or "\0" in c
                       for c in [*collections, *memory_collections])):
            return None
        path = _db_path(index)
        if path is None:
            return None
        left = max(0, deadline - time.monotonic())
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=min(.05, left), isolation_level=None)
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("PRAGMA query_only=ON")
        if not _schema_supported(db) or time.monotonic() >= deadline:
            return None
        available = True
        memories = set(memory_collections)
        scopes = ([(c, c in memories) for c in dict.fromkeys([*collections, *memory_collections])]
                  if collections else [(None, False), *[(c, True) for c in dict.fromkeys(memory_collections)]])
        for scope, is_memory in scopes:
            for rung_index, query in rungs:
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"calls": calls, "complete": False, "reason": "deadline"}
                db.execute(f"PRAGMA busy_timeout={min(50, max(0, int(left * 1000)))}")
                rows = _rows(db, query, scope, max(1, min(100, int(limit))))
                if time.monotonic() >= deadline:
                    return {"calls": calls, "complete": False, "reason": "deadline"}
                calls.append((scope, is_memory, rung_index, rows))
        return {"calls": calls, "complete": True, "reason": ""}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        if not available:
            return None
        return {"calls": calls, "complete": False,
                "reason": "deadline" if time.monotonic() >= deadline else "sqlite_unavailable"}
    finally:
        if db is not None:
            db.close()
