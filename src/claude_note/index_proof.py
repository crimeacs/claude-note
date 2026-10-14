"""Read-only proof for tracked revisions, never index/model execution.

Successful process exits alone are insufficient: QMD 2.8.3 can skip unreadable
files and return success for a busy/partially failed embed. Results here describe
only the supplied captured revisions, not the completeness of an entire corpus.
"""

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import setup_recall

TIMEOUT_SECONDS = 8.0
MAX_REFERENCES = 1000
MAX_BODY_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
SQLITE_HEAP_BYTES = 16 * 1024 * 1024
SQLITE_VALUE_BYTES = MAX_BODY_BYTES + 64 * 1024


def _result(lexical=False, vector=False, reason=''):
    return {'lexical': lexical, 'vector': vector, 'reason': reason}


def _references(references):
    if not isinstance(references, (tuple, list)) or not 0 < len(references) <= MAX_REFERENCES:
        raise ValueError('No bounded captured revisions to verify')
    result, seen = [], set()
    for ref in references:
        if not isinstance(ref, dict) or set(ref) != {'collection', 'path', 'revision'}:
            raise ValueError('Invalid captured revision schema')
        collection, path, revision = (ref[key] for key in ('collection', 'path', 'revision'))
        if (not isinstance(collection, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', collection)
                or not isinstance(path, str) or not path or len(path.encode()) > 4096
                or any(ord(c) < 32 for c in path) or Path(path).is_absolute()
                or any(part in ('', '.', '..') for part in path.split('/'))
                or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision)):
            raise ValueError('Invalid captured revision reference')
        key = (collection, path)
        if key in seen:
            raise ValueError('Duplicate captured revision reference')
        seen.add(key)
        result.append(dict(ref))
    return tuple(result)


def _deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('Read-only index proof deadline')


def _lexical(database, references, deadline):
    """No source-file reads; indexed bodies stay within this process."""
    _deadline(deadline)
    if not database.is_absolute() or not database.is_file():
        raise ValueError('Bound index database is unavailable')
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.1)
    try:
        # SQLite length(CAST(TEXT AS BLOB)) can materialize the whole value.
        # These limits precede schema/body reads and apply only in this
        # disposable reader, never the long-lived index/recall process.
        heap = db.execute(f'PRAGMA hard_heap_limit={SQLITE_HEAP_BYTES}').fetchone()
        if not heap or type(heap[0]) is not int or not 0 < heap[0] <= SQLITE_HEAP_BYTES:
            raise ValueError('Read-only SQLite allocator cap is unavailable')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQLITE_VALUE_BYTES)
        if not 0 < db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) <= SQLITE_VALUE_BYTES:
            raise ValueError('Read-only SQLite value cap is unavailable')
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute('BEGIN')
        for table, columns in {'documents': {'id', 'collection', 'path', 'title', 'hash', 'active'},
                               'content': {'hash', 'doc'},
                               'documents_fts': {'filepath', 'title', 'body'}}.items():
            definition = db.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (table,)).fetchone()
            if (not definition or definition[0] != 'table' or not columns.issubset(
                    {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')})):
                raise ValueError('Unsupported read-only lexical schema')
            if table == 'documents_fts' and (not re.search(r'\bUSING\s+fts5\s*\(', definition[1] or '', re.I)
                                           or re.search(r'\bcontent\s*=', definition[1] or '', re.I)):
                raise ValueError('Expected standalone QMD FTS5 schema')
        total = 0
        for ref in references:
            _deadline(deadline)
            rows = db.execute('SELECT id,hash,length(CAST(title AS BLOB)),active FROM documents WHERE collection=? AND path=? LIMIT 2',
                              (ref['collection'], ref['path'])).fetchall()
            if len(rows) != 1 or rows[0][1] != ref['revision'] or rows[0][3] != 1:
                raise ValueError('Captured revision is absent from the active text index')
            document_id, _, title_length, _ = rows[0]
            if type(title_length) is not int or not 0 <= title_length <= 16384:
                raise ValueError('Indexed title exceeds its proof bound')
            title = db.execute('SELECT title FROM documents WHERE id=?', (document_id,)).fetchone()[0]
            length = db.execute('SELECT length(CAST(doc AS BLOB)) FROM content WHERE hash=?',
                                (ref['revision'],)).fetchall()
            if len(length) != 1 or type(length[0][0]) is not int or not 0 < length[0][0] <= MAX_BODY_BYTES:
                raise ValueError('Captured indexed content is missing or exceeds its bound')
            total += length[0][0]
            if total > MAX_TOTAL_BYTES:
                raise ValueError('Captured indexed content exceeds total proof bound')
            body = db.execute('SELECT doc FROM content WHERE hash=?', (ref['revision'],)).fetchone()[0]
            if not isinstance(body, str) or hashlib.sha256(body.encode('utf-8')).hexdigest() != ref['revision']:
                raise ValueError('Indexed content does not match the captured revision')
            # The pinned writer normalizes CJK runs in FTS. Exact raw equality
            # remains accepted for its documented legacy trigger path. When
            # normalization changed the body this Python-only proof stays
            # conservative; the Node observer validates the exact SDK transform.
            lengths = db.execute('SELECT length(CAST(filepath AS BLOB)),length(CAST(title AS BLOB)),length(CAST(body AS BLOB)) '
                                 'FROM documents_fts WHERE rowid=? LIMIT 2', (document_id,)).fetchall()
            if (len(lengths) != 1 or any(type(value) is not int or not 0 <= value <= bound
                    for value, bound in zip(lengths[0], (16384, 16384, MAX_BODY_BYTES)))):
                raise ValueError('Indexed FTS row exceeds its proof bound')
            fts = db.execute('SELECT filepath,title,body FROM documents_fts WHERE rowid=? LIMIT 2',
                             (document_id,)).fetchall()
            expected = (ref['collection'] + '/' + ref['path'], title, body)
            if len(fts) != 1 or fts[0] != expected:
                raise ValueError('Captured revision lacks an exact FTS row')
        _deadline(deadline)
    finally:
        db.close()


def _run(payload, command, deadline):
    body = json.dumps(payload, ensure_ascii=False).encode()
    if len(body) > MAX_INPUT_BYTES:
        raise ValueError('Read-only observer input exceeds its bound')
    _deadline(deadline)
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'NODE_OPTIONS', 'NODE_PATH', 'BUN_OPTIONS', 'PYTHONSTARTUP', 'PYTHONPATH'}}
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True, env=environment)
    output = bytearray()
    try:
        # A temporary regular file is unnecessary: nonblocking pipes bound both
        # incoming references and a malfunctioning native dependency's output.
        for handle in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(handle.fileno(), False)
        sent, stderr_size = 0, 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                _deadline(deadline)
                for key, _ in selector.select(min(.05, max(0, deadline - time.monotonic()))):
                    handle, kind = key.fileobj, key.data
                    if kind == 'stdin':
                        try:
                            sent += os.write(handle.fileno(), body[sent:sent + 65536])
                        except BrokenPipeError:
                            selector.unregister(handle)
                            handle.close()
                            continue
                        if sent == len(body):
                            selector.unregister(handle)
                            handle.close()
                    else:
                        chunk = os.read(handle.fileno(), 65536)
                        if not chunk:
                            selector.unregister(handle)
                        elif kind == 'stdout':
                            output.extend(chunk)
                        else:
                            stderr_size += len(chunk)
                        if len(output) > MAX_OUTPUT_BYTES or stderr_size > MAX_OUTPUT_BYTES:
                            raise ValueError('Read-only observer output exceeds its bound')
        process.wait(timeout=max(.001, deadline - time.monotonic()))
        if process.returncode != 0:
            raise ValueError('Read-only native observer unavailable')
        result = json.loads(output)
        if (not isinstance(result, dict) or set(result) != {'lexical', 'vector', 'reason'}
                or type(result['lexical']) is not bool or type(result['vector']) is not bool
                or (result['vector'] and not result['lexical'])
                or not isinstance(result['reason'], str) or len(result['reason']) > 512):
            raise ValueError('Invalid read-only native observer result')
        return result
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for handle in (process.stdin, process.stdout, process.stderr):
            if not handle.closed:
                handle.close()
        process.wait(timeout=.5)


def _observer(payload, node, deadline):
    return _run(payload, [node, str(Path(__file__).with_suffix('.mjs'))], deadline)


def _lexical_main():
    """Disposable Python reader: the parent can terminate even blocked IO."""
    try:
        body = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(body) > MAX_INPUT_BYTES:
            raise ValueError('Lexical proof input bound')
        payload = json.loads(body)
        remaining = payload['remaining']
        if type(remaining) not in (float, int) or not 0 < remaining <= TIMEOUT_SECONDS:
            raise ValueError('Lexical proof deadline bound')
        _lexical(Path(payload['database']), _references(payload['references']), time.monotonic() + remaining)
        answer = _result(True)
    except (OSError, ValueError, TypeError, KeyError, MemoryError, sqlite3.Error, TimeoutError):
        answer = _result(reason='Captured lexical revision proof failed')
    print(json.dumps(answer))


def verify(*, database: str, configuration: str, qmd: str, references: tuple, vector: bool) -> dict:
    """One deadline across all references; unsupported schemas fail closed."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    lexical = False
    try:
        if type(vector) is not bool:
            raise ValueError('Invalid vector proof flag')
        references = _references(references)
        database = Path(database)
        code = ('import sys; sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1]))
                + '); from claude_note.index_proof import _lexical_main; _lexical_main()')
        text = _run({'database': str(database), 'references': references,
                     'remaining': max(.001, deadline - time.monotonic())},
                    [sys.executable, '-B', '-I', '-c', code], deadline)
        lexical = text['lexical']
        if lexical and not vector:
            return _result(True)
        sdk, node = setup_recall.qmd_sdk(qmd) if qmd else None, shutil.which('node')
        if not sdk or not node:
            return _result(lexical, reason='Pinned read-only native index proof is unavailable')
        result = _observer({'database': str(database), 'configuration': str(configuration),
                            'sdk': str(sdk), 'references': references, 'vector': vector}, node, deadline)
        # If the database changed between read snapshots, prefer the later
        # observer's conservative result over an earlier successful text proof.
        return result
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, TimeoutError):
        return _result(lexical, reason='Read-only index proof failed or exceeded its bound')
