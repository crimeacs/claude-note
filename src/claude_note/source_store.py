"""Scoped immutable transcript chunks; legacy source snapshots remain untouched.

Only digest-derived paths enter the store. A manifest becomes visible after all
its chunks are durable. Orphan chunks survive interrupted publication and count
against the same global budget; this module never prunes evidence.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import config, state_io

TARGET_CHUNK_BYTES = 256 * 1024
MAX_RAW = 256 * 1024 * 1024
MAX_SEGMENTS = 1024
MAX_MANIFEST = 1024 * 1024
MAX_STORE_BYTES = 4 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
MAX_STORE_ENTRIES = 65536
ENTRY_OVERHEAD = 4096
READ_BYTES = 1024 * 1024
LOCK_SECONDS = 5
HEX = re.compile(r"[0-9a-f]{64}\Z")


class SourceStoreError(ValueError):
    """Stored evidence or bounded publication could not be proved safe."""


def _identity(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise SourceStoreError("Invalid source store identity")
    return value


def _state(state_dir=None):
    return state_io._path(config.STATE_DIR if state_dir is None else state_dir)


def _root(state_dir=None):
    return _state(state_dir) / "sources" / "objects"


def manifest_path(revision, scope):
    return _root() / _identity(scope) / (_identity(revision) + ".source.json")


def _chunk_path(revision, scope):
    return _root() / _identity(scope) / "chunks" / (_identity(revision) + ".jsonl")


def _lines(raw):
    return raw.count(b"\n") + bool(raw and not raw.endswith(b"\n"))


def _unsigned(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceStoreError("Duplicate source manifest field")
        result[key] = value
    return result


def _info(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _parts(path, expected_size=None, maximum=MAX_RAW):
    """Bounded, stable, descriptor-confined reads; never follow a final link."""
    with state_io._parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum
                    or expected_size is not None and before.st_size != expected_size):
                raise SourceStoreError("Invalid bounded source file")
            total = 0
            while True:
                raw = handle.read(min(READ_BYTES, maximum - total + 1))
                if not raw:
                    break
                total += len(raw)
                if total > maximum:
                    raise SourceStoreError("Source file exceeds bound")
                yield raw
            after = os.fstat(handle.fileno())
            if _info(before) != _info(after) or total != before.st_size:
                raise SourceStoreError("Source file changed during verification")


def _manifest(path, expected_sha, scope):
    expected_sha, scope = _identity(expected_sha), _identity(scope)
    if state_io._path(path) != manifest_path(expected_sha, scope):
        raise SourceStoreError("Source manifest path does not match identity")
    raw = b"".join(_parts(path, maximum=MAX_MANIFEST))
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(SourceStoreError("Invalid source number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SourceStoreError("Invalid source manifest") from exc
    keys = {"version", "scope", "captured_revision", "bytes", "lines", "chunks"}
    if (not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int or value["version"] != 1
            or value["scope"] != scope or value["captured_revision"] != expected_sha
            or not _unsigned(value["bytes"], MAX_RAW) or not _unsigned(value["lines"], MAX_RAW)
            or not isinstance(value["chunks"], list) or len(value["chunks"]) > MAX_SEGMENTS):
        raise SourceStoreError("Invalid source manifest schema")
    size = lines = 0
    for row in value["chunks"]:
        if (not isinstance(row, dict) or set(row) != {"revision", "bytes", "lines"}
                or not _unsigned(row["bytes"], MAX_RAW) or row["bytes"] == 0
                or not _unsigned(row["lines"], row["bytes"]) or row["lines"] == 0):
            raise SourceStoreError("Invalid source chunk schema")
        _identity(row["revision"])
        size += row["bytes"]
        lines += row["lines"]
        if size > MAX_RAW or lines > MAX_RAW:
            raise SourceStoreError("Source manifest totals exceed bounds")
    if size != value["bytes"] or lines != value["lines"] or bool(size) != bool(value["chunks"]):
        raise SourceStoreError("Source manifest totals do not match")
    return value


def _verified(path, expected_sha, scope, sink=None):
    value = _manifest(path, expected_sha, scope)
    full = hashlib.sha256()
    for index, row in enumerate(value["chunks"]):
        chunk, count, last = hashlib.sha256(), 0, b""
        for raw in _parts(_chunk_path(row["revision"], scope), row["bytes"]):
            chunk.update(raw)
            full.update(raw)
            count += raw.count(b"\n")
            last = raw[-1:]
            if sink is not None:
                sink(raw)
        if last and last != b"\n":
            count += 1
        if (chunk.hexdigest() != row["revision"] or count != row["lines"]
                or index < len(value["chunks"]) - 1 and last != b"\n"):
            raise SourceStoreError("Source chunk identity or line boundary mismatch")
    if full.hexdigest() != expected_sha:
        raise SourceStoreError("Concatenated source identity mismatch")
    return value


def verify(path, expected_sha, *, scope):
    """Verify all bytes with <=1 MiB read buffers and return manifest metadata."""
    return _verified(path, expected_sha, scope)


def read(path, expected_sha, *, scope):
    """Reconstruct only after strict bounded metadata validation; verify each use."""
    body = bytearray()
    _verified(path, expected_sha, scope, body.extend)
    return bytes(body)


def segments(path, expected_sha, *, scope):
    """Verified real JSONL files with one-based source-global starting lines."""
    value = verify(path, expected_sha, scope=scope)
    result, start = [], 1
    for row in value["chunks"]:
        result.append({"path": str(_chunk_path(row["revision"], scope)), "revision": row["revision"],
                       "start_line": start, "lines": row["lines"], "bytes": row["bytes"]})
        start += row["lines"]
    return result


def _plan(raw, scope):
    chunks, start, cursor = [], 0, 0
    while cursor < len(raw):
        newline = raw.find(b"\n", cursor)
        end = len(raw) if newline < 0 else newline + 1
        cursor = end
        if cursor - start >= TARGET_CHUNK_BYTES or cursor == len(raw):
            body = memoryview(raw)[start:cursor]
            count = raw.count(b"\n", start, cursor) + (raw[cursor-1:cursor] != b"\n")
            chunks.append((start, cursor, {"revision": hashlib.sha256(body).hexdigest(), "bytes": len(body), "lines": count}))
            start = cursor
        if len(chunks) > MAX_SEGMENTS:
            raise SourceStoreError("Source has too many chunks")
    value = {"version": 1, "scope": scope, "captured_revision": hashlib.sha256(raw).hexdigest(),
             "bytes": len(raw), "lines": _lines(raw), "chunks": [row for _, _, row in chunks]}
    manifest = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(manifest) > MAX_MANIFEST:
        raise SourceStoreError("Source manifest exceeds bound")
    return chunks, value, manifest


@contextmanager
def _store_lock(state_dir=None, *, deadline=None):
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise SourceStoreError("Invalid source store deadline")
    end = min(time.monotonic() + LOCK_SECONDS, deadline) if deadline is not None else time.monotonic() + LOCK_SECONDS
    if time.monotonic() >= end:
        raise TimeoutError("Source store publication lock deadline")
    fd = state_io.open_lock(_root(state_dir) / ".store.lock")
    try:
        while True:
            if time.monotonic() >= end:
                raise TimeoutError("Source store publication lock deadline")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("Source store publication lock deadline")
                time.sleep(.01)
        yield
    finally:
        os.close(fd)


def _allocated(info):
    return max(info.st_size, getattr(info, "st_blocks", 0) * 512) + ENTRY_OVERHEAD


def _usage(state_dir=None):
    total, count = 0, 0
    def visit(fd, depth):
        nonlocal total, count
        if depth > 8:
            raise SourceStoreError("Source store directory depth exceeds bound")
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > MAX_STORE_ENTRIES:
                    raise SourceStoreError("Source store metadata exceeds bound")
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                total += _allocated(info)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, depth + 1)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SourceStoreError("Unsafe source store entry")
    state = _state(state_dir)
    roots = [_root(state_dir), state / ".claude-note" / "provenance",
             state / "import-streams", state / "import-intents", state / "import-publications"]
    if state_dir is None or state == _state():
        # An arbitrary source-state root must never pull the owner's queue
        # into a temporary privacy projection or budget calculation.
        roots.append(state_io._path(config.QUEUE_DIR))
    for root in dict.fromkeys(roots):
        with ExitStack() as stack:
            try:
                fd, _ = stack.enter_context(state_io._parent(root / ".scan"))
            except FileNotFoundError:
                # Only the absent root is optional; mutations during the scan
                # propagate rather than turning a partial scan into admission.
                continue
            total += _allocated(os.fstat(fd))
            count += 1
            visit(fd, 0)
    return total, count


def _free_bytes(state_dir=None):
    with state_io._parent(_root(state_dir) / ".space") as (fd, _):
        space = os.fstatvfs(fd)
        return space.f_bavail * space.f_frsize


def _allocation_unit(state_dir=None):
    with state_io._parent(_root(state_dir) / ".allocation") as (fd, _):
        space = os.fstatvfs(fd)
        # f_bsize is preferred IO size (1 MiB on some APFS volumes), while
        # f_frsize is the allocation unit used by the free-space counters.
        return max(ENTRY_OVERHEAD, space.f_frsize or space.f_bsize)


def _matches(path, expected):
    # Only an already-existing exact artifact may use state_io's own
    # intent-bound interrupted-hardlink recovery. Ordinary reads reject it.
    try:
        with state_io._parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if info.st_nlink == 2:
                    if not stat.S_ISREG(info.st_mode) or info.st_size != len(expected):
                        raise SourceStoreError("Invalid interrupted source publication")
                    offset = 0
                    while True:
                        part = handle.read(READ_BYTES)
                        if not part:
                            break
                        if part != expected[offset:offset + len(part)]:
                            raise SourceStoreError("Interrupted source publication bytes differ")
                        offset += len(part)
                    if _info(info) != _info(os.fstat(handle.fileno())) or offset != len(expected):
                        raise SourceStoreError("Interrupted source publication changed")
                    state_io.create_bytes(path, bytes(expected))
    except FileNotFoundError:
        return False
    offset = 0
    try:
        for raw in _parts(path, len(expected)):
            if raw != expected[offset:offset + len(raw)]:
                raise SourceStoreError("Immutable source identity collision or corruption")
            offset += len(raw)
        return True
    except FileNotFoundError:
        return False


def _reserve(used, entries, sizes, *, state_dir=None):
    # Include chunk/manifest lock, durable intent and atomic intent temporary,
    # plus directory growth. Physical and logical allocation are both bounded.
    unit = _allocation_unit(state_dir)
    allocation = sum(((size + unit - 1) // unit) * unit + 4 * (unit + ENTRY_OVERHEAD) for size in sizes)
    allocation += 4 * (unit + ENTRY_OVERHEAD)
    if entries + 4 * len(sizes) + 4 > MAX_STORE_ENTRIES or used + allocation > MAX_STORE_BYTES:
        raise SourceStoreError("Source store budget exceeded")
    if _free_bytes(state_dir) < MIN_FREE_BYTES + allocation:
        raise SourceStoreError("Insufficient source store headroom")


@contextmanager
def auxiliary_admission(sizes, *, state_dir=None, deadline=None):
    """Reserve a bounded durable source-lineage batch under the global lock.

    Callers hold their lineage locks, provide conservative complete new-record
    sizes and keep this context across every write. No ledger body is read here.
    """
    if (not isinstance(sizes, list) or len(sizes) > MAX_SEGMENTS + 1
            or any(not _unsigned(size, MAX_RAW) for size in sizes)):
        raise SourceStoreError("Invalid auxiliary source storage sizes")
    with _store_lock(state_dir, deadline=deadline):
        if sizes:
            used, entries = _usage(state_dir)
            _reserve(used, entries, sizes, state_dir=state_dir)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Auxiliary source storage admission deadline")
        yield


def publish(raw, *, scope):
    """Publish an immutable scoped manifest after dedupe, budget and free-space proof."""
    _identity(scope)
    if not isinstance(raw, bytes) or len(raw) > MAX_RAW:
        raise SourceStoreError("Source exceeds bounded local storage")
    chunks, value, manifest = _plan(raw, scope)
    path = manifest_path(value["captured_revision"], scope)
    with _store_lock():
        missing, seen = [], set()
        for start, end, row in chunks:
            revision = row["revision"]
            if revision in seen:
                continue
            seen.add(revision)
            target = _chunk_path(revision, scope)
            if not _matches(target, memoryview(raw)[start:end]):
                missing.append((target, start, end))
        complete = _matches(path, manifest)
        if complete:
            # A manifest referring to absent chunks is corruption, not repair authority.
            if missing:
                raise SourceStoreError("Published source chunk is missing")
            verify(path, value["captured_revision"], scope=scope)
            return path
        used, entries = _usage()
        _reserve(used, entries, [end-start for _, start, end in missing] + [len(manifest)])
        for target, start, end in missing:
            state_io.create_bytes(target, raw[start:end])
        # Exact existing bytes are checked above; create_bytes also verifies any
        # concurrent same-name publication and fsyncs each new immutable file.
        state_io.create_bytes(path, manifest)
        verify(path, value["captured_revision"], scope=scope)
        return path
