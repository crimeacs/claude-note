"""Private, bounded descriptor-relative state IO; Markdown uses mutation.py."""

import json
import os
import stat
import sys
import uuid
import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

LIMIT = 16_000_000


def _path(path):
    path = Path(path).absolute()
    # macOS documents these root aliases; do not generalize to user symlinks.
    if sys.platform == 'darwin' and len(path.parts) > 1 and path.parts[1] in {'var', 'tmp', 'etc'}:
        alias = Path('/') / path.parts[1]
        expected = Path('/private') / path.parts[1]
        if alias.is_symlink() and alias.resolve() == expected:
            path = expected.joinpath(*path.parts[2:])
    if any(part in {'.', '..'} for part in path.parts):
        raise ValueError('Invalid state path')
    return path


@contextmanager
def _parent(path, create=False):
    path = _path(path)
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value) -> None:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()
    if len(body) > LIMIT:
        raise ValueError('State record exceeds 16 MB')
    atomic_bytes(path, body)


def atomic_bytes(path, body):
    if not isinstance(body, bytes) or len(body) > 256 * 1024 * 1024:
        raise ValueError('Local payload exceeds 256 MB')
    with _parent(path, True) as (parent, name):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('State target must be a singly linked regular file')
        except FileNotFoundError:
            pass
        temporary = '.claude-note-' + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def append_line(path, body):
    if len(body) > 4 * 1024 * 1024 or not body.endswith(b'\n'):
        raise ValueError('Queue line exceeds bounded capture payload')
    with _parent(path, True) as (parent, name):
        descriptor = _open_or_create(parent, name, os.O_WRONLY | os.O_APPEND)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Queue must be a singly linked regular file')
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            remaining = memoryview(body)
            while remaining:
                remaining = remaining[os.write(descriptor, remaining):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _open_or_create(parent, name, flags):
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    for attempt in range(3):
        try:
            return os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            try:
                return os.open(name, flags, dir_fd=parent)
            except FileNotFoundError:
                if attempt == 2:
                    raise
    raise FileNotFoundError(name)


def open_lock(path):
    with _parent(path, True) as (parent, name):
        descriptor = _open_or_create(parent, name, os.O_WRONLY)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("Lock must be a singly linked regular file")
    return descriptor


def create_bytes(path, body):
    """Publish immutable evidence exclusively; an existing digest must match."""
    path = Path(path)
    descriptor = open_lock(path.with_name(path.name + ".lock"))
    deadline = time.monotonic() + 5
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Immutable evidence publication lock deadline")
                time.sleep(.01)
        _create_bytes(path, body)
    finally:
        os.close(descriptor)


def _create_bytes(path, body):
    if not isinstance(body, bytes) or len(body) > 256 * 1024 * 1024:
        raise ValueError("Evidence snapshot exceeds bounded local storage")
    import hashlib
    intent_path = Path(path).with_name(Path(path).name + ".intent.json")
    expected = hashlib.sha256(body).hexdigest()
    with _parent(path, True) as (parent, name):
        def verify_existing():
            existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(existing, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != len(body) or handle.read(len(body) + 1) != body:
                    raise ValueError("Immutable evidence identity collision or corruption")
                if info.st_nlink == 2:
                    try:
                        intent = read_json(intent_path)
                        orphan = intent["temporary"]
                        if (not orphan.startswith(".claude-note-") or uuid.UUID(orphan[13:]).hex != orphan[13:]
                                or intent["revision"] != expected):
                            raise ValueError("Invalid publication intent")
                        linked = os.stat(orphan, dir_fd=parent, follow_symlinks=False)
                        if (not stat.S_ISREG(linked.st_mode) or (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino)):
                            raise ValueError("Publication temporary does not match")
                        os.unlink(orphan, dir_fd=parent)
                        os.fsync(parent)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        raise ValueError("Unrecognized evidence hardlink") from exc
                elif info.st_nlink != 1:
                    raise ValueError("Unrecognized evidence hardlink")
        try:
            verify_existing()
            return
        except FileNotFoundError:
            pass
        temporary = ".claude-note-" + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            atomic_json(intent_path, {"version": 1, "temporary": temporary, "revision": expected, "size": len(body)})
            try:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            except FileExistsError:
                verify_existing()
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def read_json(path: Path):
    with _parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > LIMIT:
                raise ValueError('Invalid state record')
            return json.load(handle)
