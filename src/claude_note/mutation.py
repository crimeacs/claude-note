"""Confined, replay-aware Markdown mutations.

This module is safe to import before configuration exists. Paths are opened from
the trusted vault descriptor with no-follow directory/final-file operations.
Human-owned files receive physical append-only additions or immutable proposals;
they are never rewritten by an unattended replacement. Ownership receipts are an
editing contract for files created here, not an authorization or approval claim.
An arbitrary editor does not honor our lock: revision checks on machine-owned
files cannot promise safety against an editor that races the final rename.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

MAX_NOTE_BYTES = 8 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 100000
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def revision(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def operation_id(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class MutationResult:
    path: Path
    status: str
    operation_id: str
    revision: str | None
    staged_path: Path | None = None
    reason: str = ''

    @property
    def applied(self) -> bool:
        return self.status in ('created', 'updated', 'unchanged')


class MutationConflict(RuntimeError):
    def __init__(self, result: MutationResult):
        self.result = result
        super().__init__(f'{result.reason}; proposal saved at {result.staged_path}')


@dataclass(frozen=True)
class MutationProposal:
    content: str
    reason: str


def _vault(vault_root: Path | None) -> Path:
    if vault_root is None:
        from . import config
        vault_root = config.VAULT_ROOT
    root = Path(vault_root).resolve()
    if sys.platform == 'darwin' and root.exists():
        descriptor = os.open(root, _DIRECTORY_FLAGS)
        try:
            # F_GETPATH returns the volume's spelling, including root aliases.
            root = Path(fcntl.fcntl(descriptor, 50, bytes(1024)).split(b'\0', 1)[0].decode())
        finally:
            os.close(descriptor)
    return root


def _fold(path: str) -> str:
    return unicodedata.normalize('NFC', path).casefold()


def _canonical_relative(root: Path, relative: Path) -> Path:
    """Use actual directory spelling for aliases without merging distinct files.

    Folded names nominate entries, but device/inode equality decides identity.
    This works on case-sensitive volumes too. Missing suffixes retain the user's
    spelling until exclusive creation; the folded lock serializes that race.
    """
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return relative
    parts = []
    try:
        for index, requested in enumerate(relative.parts):
            try:
                info = os.stat(requested, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return Path(*parts, *relative.parts[index:])
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('Refusing a linked path component')
            actual = None
            with os.scandir(descriptor) as entries:
                for count, entry in enumerate(entries, 1):
                    if count > MAX_DIRECTORY_ENTRIES:
                        raise ValueError('Directory exceeds bounded identity inspection')
                    if _fold(entry.name) != _fold(requested):
                        continue
                    candidate = entry.stat(follow_symlinks=False)
                    if (candidate.st_dev, candidate.st_ino) == (info.st_dev, info.st_ino):
                        actual = entry.name
                        break
            if actual is None:
                raise ValueError('Path identity changed while canonicalizing')
            parts.append(actual)
            if index < len(relative.parts) - 1:
                child = os.open(actual, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        return Path(*parts)
    finally:
        os.close(descriptor)


def _configured_root(vault_root: Path | None) -> Path:
    if vault_root is None:
        from . import config
        return Path(config.VAULT_ROOT)
    return Path(vault_root)


def resolve_note_path(path: str | Path, vault_root: Path, *, allow_protected: bool = False) -> Path:
    """Validate lexical policy; descriptor traversal enforces it again on use."""
    original_root = Path(vault_root).absolute()
    root = _vault(original_root)
    raw = str(path)
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(original_root)
        except ValueError:
            try:
                # macOS system aliases are trusted platform paths, not user
                # symlinks. Canonicalize only this prefix, preserving every
                # child component for the no-follow policy below.
                if sys.platform == 'darwin' and len(candidate.parts) > 1 and candidate.parts[1] in {'var', 'tmp', 'etc'}:
                    alias = Path('/') / candidate.parts[1]
                    expected = Path('/private') / candidate.parts[1]
                    if alias.is_symlink() and alias.resolve() == expected:
                        candidate = expected.joinpath(*candidate.parts[2:])
                candidate = candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError('Note path must stay inside the vault') from exc
        raw = str(candidate)
    if not raw or raw != raw.strip() or '\\' in raw or ':' in raw or any(ord(c) < 32 for c in raw):
        raise ValueError(f'Invalid note path: {raw!r}')
    if any(p in ('', '.', '..') for p in raw.split('/')):
        raise ValueError(f'Note path must stay inside the vault: {raw!r}')
    if not allow_protected:
        if any(p.startswith('.') for p in candidate.parts):
            raise ValueError('Hidden paths are reserved')
        if not candidate.suffix:
            candidate = candidate.with_suffix('.md')
        elif candidate.suffix.lower() != '.md':
            raise ValueError('Note path must use the .md extension')
        legacy = re.fullmatch(r'claude-session-\d{4}-\d{2}-\d{2}-.+\.md', candidate.name, re.IGNORECASE)
        if (candidate.parts[0].lower() in ('templates', 'sessions')
                or candidate.name.lower() in ('claude.md', 'agents.md') or legacy):
            raise ValueError('Note path targets a protected vault file')
    target = root / candidate
    # Catch existing and dangling symlinks even for read-free/preflight callers.
    for component in (target, *target.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError(f'Note path traverses a symlink: {raw!r}')
    return root / _canonical_relative(root, candidate)


@contextmanager
def _parent(root: Path, relative: Path, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Open each component relative to its verified directory, never via a link."""
    fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        for part in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError('Refusing a linked/non-directory path component') from exc
                raise
            os.close(fd)
            fd = child
        yield fd, relative.name
    finally:
        os.close(fd)


def _read_at(parent: int, name: str, *, allowed_links=(1,)) -> tuple[str | None, os.stat_result | None]:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError('Refusing a linked target') from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink not in allowed_links:
            raise ValueError('Mutation target must be a regular, singly linked file')
        if info.st_size > MAX_NOTE_BYTES:
            raise ValueError('Mutation target exceeds the bounded note size')
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, MAX_NOTE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_NOTE_BYTES:
                raise ValueError('Mutation target exceeds the bounded note size')
        return b''.join(chunks).decode('utf-8'), info
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    while data:
        count = os.write(fd, data)
        if count <= 0:
            raise OSError('Short write without progress')
        data = data[count:]
    os.fsync(fd)


def _private_write(parent: int, name: str, content: str, *, exclusive: bool = False,
                   temporary: str | None = None) -> None:
    data = content.encode('utf-8')
    if len(data) > MAX_NOTE_BYTES:
        raise ValueError('Mutation output exceeds the bounded note size')
    temporary = temporary or '.claude-note-' + uuid.uuid4().hex + '.tmp'
    if not re.fullmatch(r'\.claude-note-[a-f0-9]{32}\.tmp', temporary):
        raise ValueError('Invalid private temporary identity')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        try:
            _write_all(fd, data)
        finally:
            os.close(fd)
        if exclusive:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        else:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass  # atomic replacement already consumed the private name


def _metadata(root: Path, category: str, key: str, value: dict | None = None) -> dict | None:
    relative = Path('.claude-note/mutations') / category / (key + '.json')
    try:
        context = _parent(root, relative, create=value is not None)
        parent, name = context.__enter__()
    except FileNotFoundError:
        if value is None:
            return None
        raise
    try:
        if value is not None:
            # Metadata links are rejected before replacement as well.
            _read_at(parent, name)
            _private_write(parent, name, json.dumps(value, sort_keys=True, ensure_ascii=False) + '\n')
            return value
        content, _ = _read_at(parent, name)
        if content is None:
            return None
        try:
            result = json.loads(content)
        except ValueError as exc:
            raise ValueError('Unreadable mutation receipt; refusing to reset replay state') from exc
        if not isinstance(result, dict):
            raise ValueError('Invalid mutation receipt')
        return result
    finally:
        context.__exit__(None, None, None)


@contextmanager
def note_lock(path: Path, *, vault_root: Path | None = None, timeout: float = 30) -> Iterator[None]:
    root = _vault(vault_root)
    target = resolve_note_path(path, _configured_root(vault_root), allow_protected=True)
    # Fold only the lock namespace: aliases serialize, while distinct files on
    # case-sensitive volumes retain separate effect/ownership receipts.
    key = operation_id(str(root), _fold(target.relative_to(root).as_posix()))
    relative = Path('.claude-note/mutations/locks') / (key + '.lock')
    with _parent(root, relative, create=True) as (parent, name):
        flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            # Exclusive creation avoids concurrent O_CREAT|O_NOFOLLOW lookup
            # failures on macOS. An existing stable lock is opened separately;
            # it is never replaced or removed while another writer holds it.
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Invalid mutation lock')
            until = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= until:
                        raise TimeoutError(f'Could not acquire mutation lock for {target}')
                    time.sleep(.01)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def read_text(path: Path, *, vault_root: Path | None = None, allow_protected: bool = True) -> str | None:
    root = _vault(vault_root)
    target = resolve_note_path(path, _configured_root(vault_root), allow_protected=allow_protected)
    try:
        with _parent(root, target.relative_to(root)) as (parent, name):
            return _read_at(parent, name)[0]
    except FileNotFoundError:
        return None


def _stage(root: Path, target: Path, before: str | None, after: str, op_id: str, reason: str) -> MutationResult:
    payload = {'version': 1, 'status': 'pending', 'path': target.relative_to(root).as_posix(),
               'operation_id': op_id, 'base_revision': revision(before) if before is not None else None,
               'proposed_revision': revision(after), 'content': after, 'reason': reason}
    key = operation_id(payload)
    relative = Path('.claude-note/mutations/pending') / (key + '.json')
    from .state_io import create_bytes
    create_bytes(root / relative, (json.dumps(payload, sort_keys=True, ensure_ascii=False) + '\n').encode())
    return MutationResult(target, 'staged', op_id, payload['base_revision'], root / relative, reason)


def _recover_created_link(parent, name, intent):
    """Remove only an intent-bound temporary link left by a crashed creation."""
    temporary = intent.get('temporary', '') if intent else ''
    if not intent or intent.get('base_revision') is not None or not re.fullmatch(r'\.claude-note-[a-f0-9]{32}\.tmp', temporary):
        return
    try:
        target_info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        temporary_info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink != 2
            or (temporary_info.st_dev, temporary_info.st_ino) != (target_info.st_dev, target_info.st_ino)):
        return
    body, _ = _read_at(parent, name, allowed_links=(2,))
    if body != intent.get('content') or revision(body) != intent.get('proposed_revision'):
        raise ValueError('Crashed creation differs from its saved intent; inspect before recovery')
    os.unlink(temporary, dir_fd=parent)
    os.fsync(parent)


def mutate(path: str | Path, transform: Callable[[str | None], str | None | MutationProposal], *,
           vault_root: Path | None = None, operation_id: str | None = None,
           expected_revision: str | None = None, machine_owned: bool = False,
           allow_protected: bool = False, require_revision: bool = False) -> MutationResult:
    """Read/check/transform/commit under one lock, or save an explicit proposal.

    `machine_owned=True` is a caller assertion about its authoring boundary and
    requires an explicit expected revision for an existing unknown file. Created
    files get an ownership receipt. `require_revision` makes destructive block
    updates require an explicitly supplied base, even on a generated note.
    Callbacks must be local, bounded transforms; no model/network work under lock.
    """
    root = _vault(vault_root)
    target = resolve_note_path(path, _configured_root(vault_root), allow_protected=allow_protected)
    with note_lock(target, vault_root=root):
        # A differently-spelled alias may have been created while waiting.
        target = resolve_note_path(target, root, allow_protected=allow_protected)
        relative = target.relative_to(root)
        key = globals()['operation_id'](relative.as_posix())
        with _parent(root, relative, create=True) as (parent, name):
            op_id = operation_id or globals()['operation_id']('mutation', relative.as_posix(), uuid.uuid4().hex)
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', op_id):
                raise ValueError('Invalid operation identity')
            receipt_key = globals()['operation_id'](key, op_id)
            receipt = _metadata(root, 'effects', receipt_key)
            intent = _metadata(root, 'intents', receipt_key)
            _recover_created_link(parent, name, intent)
            before, info = _read_at(parent, name)
            if before is not None:
                # Older ledgers may predate artifact audience persistence.
                # Record existing controls before any unattended replacement
                # can remove them; later authored content cannot broaden them.
                from . import egress
                egress.record_derivation(target, before, vault_root=root)
            base = revision(before) if before is not None else None
            if receipt and receipt.get('status') == 'applied':
                return MutationResult(target, 'unchanged', op_id, base, reason='Operation already applied')
            if intent and base == intent.get('proposed_revision'):
                from . import egress, freshness
                egress.record_derivation(target, intent['content'], vault_root=root)
                freshness.mark_saved(target, base, op_id, vault_root=root)
                if intent.get('update_ownership'):
                    _metadata(root, 'ownership', key, {'version': 1, 'path': relative.as_posix(), 'revision': base})
                _metadata(root, 'effects', receipt_key, {'version': 1, 'status': 'applied',
                          'path': relative.as_posix(), 'operation_id': op_id, 'revision': base})
                return MutationResult(target, 'unchanged', op_id, base, reason='Recovered committed effect')
            if intent and base != intent.get('base_revision'):
                return _stage(root, target, before, intent['content'], op_id,
                              'Unknown partial effect or changed file; reconcile saved intent')
            after = transform(before)
            review_reason = after.reason if isinstance(after, MutationProposal) else ''
            if isinstance(after, MutationProposal):
                after = after.content
            if review_reason:
                if not isinstance(after, str) or len(after.encode('utf-8')) > MAX_NOTE_BYTES:
                    raise ValueError('Mutation proposal must be bounded UTF-8 text')
                return _stage(root, target, before, after, op_id, review_reason)
            if after is None or after == before:
                return MutationResult(target, 'unchanged', op_id, base)
            if not isinstance(after, str) or len(after.encode('utf-8')) > MAX_NOTE_BYTES:
                raise ValueError('Mutation output must be bounded UTF-8 text')
            if expected_revision is not None and expected_revision != base:
                return _stage(root, target, before, after, op_id, 'Base revision changed')
            ownership = _metadata(root, 'ownership', key)
            owned = bool(ownership and ownership.get('revision') == base)
            append_only = before is not None and after.startswith(before)
            if before is not None and not append_only:
                if require_revision and expected_revision is None:
                    return _stage(root, target, before, after, op_id, 'Replacement requires an explicit base revision')
                if not owned and not (machine_owned and expected_revision is not None):
                    return _stage(root, target, before, after, op_id, 'Human-owned or changed file; review required')
            # Re-read after the transform: callbacks cannot silently erase a
            # contemporaneous edit even in the supported machine authoring path.
            current, current_info = _read_at(parent, name)
            if current != before or (info and current_info and (info.st_dev, info.st_ino) != (current_info.st_dev, current_info.st_ino)):
                return _stage(root, target, current, after, op_id, 'File changed while preparing mutation')
            temporary = '.claude-note-' + uuid.uuid4().hex + '.tmp'
            _metadata(root, 'intents', receipt_key, {'version': 1, 'path': relative.as_posix(),
                      'operation_id': op_id, 'base_revision': base, 'content': after,
                      'proposed_revision': revision(after), 'temporary': temporary,
                      'update_ownership': before is None or owned or bool(machine_owned and expected_revision is not None)})
            from . import egress, freshness
            egress.record_derivation(target, after, vault_root=root)
            freshness.mark_pending(target, op_id, vault_root=root)
            if before is None:
                _private_write(parent, name, after, exclusive=True, temporary=temporary)
                status = 'created'
            elif append_only:
                fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    appended_info = os.fstat(fd)
                    if (not stat.S_ISREG(appended_info.st_mode) or appended_info.st_nlink != 1
                            or (appended_info.st_dev, appended_info.st_ino) != (info.st_dev, info.st_ino)):
                        return _stage(root, target, before, after, op_id, 'Append target changed')
                    _write_all(fd, after[len(before):].encode('utf-8'))
                finally:
                    os.close(fd)
                status = 'updated'
            else:
                _private_write(parent, name, after, temporary=temporary)
                status = 'updated'
            actual, _ = _read_at(parent, name)
            if actual != after:
                return _stage(root, target, actual, after, op_id, 'Concurrent external change; inspect effect intent')
            digest = revision(after)
            freshness.mark_saved(target, digest, op_id, vault_root=root)
            # An append to a human file must not quietly take ownership of it.
            if before is None or owned or (machine_owned and expected_revision is not None):
                _metadata(root, 'ownership', key, {'version': 1, 'path': relative.as_posix(), 'revision': digest})
            _metadata(root, 'effects', receipt_key, {'version': 1, 'status': 'applied',
                      'path': relative.as_posix(), 'operation_id': op_id, 'revision': digest})
            return MutationResult(target, status, op_id, digest)


def create_note(path: Path, content: str, *, vault_root: Path | None = None,
                allow_protected: bool = False, operation_id: str | None = None) -> MutationResult:
    def create(existing):
        if existing is not None:
            raise FileExistsError(path)
        return content
    return mutate(path, create, vault_root=vault_root, allow_protected=allow_protected, operation_id=operation_id)


def serialize_frontmatter(frontmatter: dict, *, plain_keys: tuple[str, ...] = ()) -> str:
    """Small safe YAML serializer: JSON scalar/list quoting is YAML compatible."""
    lines = ['---']
    for key, value in frontmatter.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', key):
            raise ValueError(f'Invalid frontmatter key: {key!r}')
        if isinstance(value, list):
            lines.append(key + ':')
            lines.extend('  - ' + json.dumps(str(item), ensure_ascii=False) for item in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError('Frontmatter numbers must be finite')
            lines.append(f'{key}: {value}')
        else:
            text = str(value)
            if (key in plain_keys and re.fullmatch(r'[A-Za-z0-9_@.+-]+', text)
                    and text.lower() not in ('true', 'false', 'null', 'yes', 'no', 'on', 'off')):
                lines.append(key + ': ' + text)
            else:
                lines.append(key + ': ' + json.dumps(text, ensure_ascii=False))
    return '\n'.join([*lines, '---'])
