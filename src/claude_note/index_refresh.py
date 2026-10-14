"""Metadata-only leases for cooperating external index refresh wrappers.

The caller owns its indexing lock and commands. Stage flags are trusted local
wrapper reports, not proof against host compromise. No index/model is invoked.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import freshness, setup_recall, state_io

STATE_ROOT = Path.home() / '.local/share/claude-note/index-refresh'
TTL_SECONDS = 2 * 60 * 60
LOCK_TIMEOUT = 5.0
MAX_LEASE_BYTES = 256 * 1024
MAX_ROOTS = 256
MAX_REFERENCES = 1000


@dataclass(frozen=True)
class IndexedRevision:
    collection: str
    path: str
    revision: str


@dataclass(frozen=True)
class CapturedGeneration:
    root: str
    generation: int
    references: tuple[IndexedRevision, ...] = ()


@dataclass(frozen=True)
class VerifiedStages:
    root: str
    lexical: bool
    vector: bool
    reason: str


@dataclass(frozen=True)
class IndexRefreshLease:
    version: int
    token: str
    index: str
    scope_identity: str
    config_identity: str
    owner_identity: str
    created_at: float
    expires_at: float
    captured: tuple[CapturedGeneration, ...]
    warning: str
    proofs: tuple[VerifiedStages, ...] = ()
    state: str = 'open'
    lexical_ok: bool | None = None
    vector_ok: bool | None = None
    completed_at: float | None = None


def _index(value):
    if not isinstance(value, str) or (value and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value)):
        raise ValueError('Index must be empty (default) or a simple named index')
    return value


def _token(value):
    if not isinstance(value, str):
        raise ValueError('Invalid index-refresh token')
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError('Invalid index-refresh token') from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError('Index-refresh token must be a canonical UUID4')
    return value


def _qmd_home():
    return Path(os.environ.get('HOME') or os.environ.get('USERPROFILE') or str(Path.home()))


def _paths(index):
    cache = Path(os.environ['XDG_CACHE_HOME']) if os.environ.get('XDG_CACHE_HOME') else setup_recall.HOME / '.cache'
    directory = Path(os.environ['QMD_CONFIG_DIR']) if os.environ.get('QMD_CONFIG_DIR') else (
        Path(os.environ.get('XDG_CONFIG_HOME') or str(setup_recall.recall_mod.HOME / '.config')) / 'qmd')
    return (cache / 'qmd' / ((index or 'index') + '.sqlite'), directory / ((index or 'index') + '.yml'))


def owner_identity(index=''):
    """One canonical index owner per journal, independent of collection edits."""
    index = _index(index)
    database, configuration = _paths(index)
    return hashlib.sha256(json.dumps([index, str(database.resolve()), str(configuration.resolve())]).encode()).hexdigest()


def _proof_identity(scope, configuration):
    return hashlib.sha256((scope + '\0' + configuration).encode()).hexdigest()


def proof_identity(index=''):
    """Validity of a completion receipt for the currently selected configuration."""
    index = _index(index)
    scope, warning = _scope(index)
    _, configuration = setup_recall._freshness_config(index)
    if warning or not configuration:
        raise ValueError('Current QMD selection cannot establish a proof scope')
    return _proof_identity(scope, configuration)


def _scope(index):
    """Fail closed where the QMD 2.8.3 CLI may select a different config/DB."""
    values = {key: os.environ.get(key, '') for key in
              ('QMD_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'INDEX_PATH', 'QMD_EMBED_MODEL')}
    warning = ''
    if values['INDEX_PATH']:
        warning = 'INDEX_PATH overrides are unsupported; no freshness will be acknowledged.'
    for key in ('QMD_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
        if values[key] and not Path(values[key]).is_absolute():
            warning = f'Relative {key} is ambiguous; no freshness will be acknowledged.'
    local = ''
    # Explicit --index selects the named global config in the pinned CLI;
    # otherwise ancestor project-local configs take precedence over env dirs.
    if not index:
        current = Path.cwd().resolve()
        for root in (current, *current.parents):
            for name in ('index.yaml', 'index.yml'):
                candidate = root / '.qmd' / name
                if candidate.exists() or candidate.is_symlink():
                    local = str(candidate)
                    warning = 'Project-local QMD configuration is unsupported; no freshness will be acknowledged.'
                    break
            if local:
                break
    default_config = not values['QMD_CONFIG_DIR'] and not values['XDG_CONFIG_HOME']
    default_cache = not values['XDG_CACHE_HOME']
    home = _qmd_home() if default_config or default_cache else None
    if home is not None and (not home.is_absolute()
            or (default_config and home.resolve() != setup_recall.recall_mod.HOME.resolve())
            or (default_cache and home.resolve() != setup_recall.HOME.resolve())):
        warning = 'QMD default home differs from the capture runtime; no freshness will be acknowledged.'
    cache = Path(values['XDG_CACHE_HOME']) if values['XDG_CACHE_HOME'] else home / '.cache'
    database = cache / 'qmd' / ((index or 'index') + '.sqlite')
    directory = Path(values['QMD_CONFIG_DIR']) if values['QMD_CONFIG_DIR'] else (
        Path(values['XDG_CONFIG_HOME'] or str(setup_recall.recall_mod.HOME / '.config')) / 'qmd')
    configuration = directory / ((index or 'index') + '.yml')
    roots = [] if warning else sorted((name, str(path.expanduser().resolve()))
                                     for name, path in setup_recall.registered_collections(index).items())
    identity = hashlib.sha256(json.dumps([index, values, str(database.resolve()),
                                        str(configuration.resolve()), roots, local,
                                        str(home.resolve()) if home is not None else None],
                                        sort_keys=True).encode()).hexdigest()
    return identity, warning


def _path(token):
    return STATE_ROOT / (_token(token) + '.json')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate index-refresh lease field')
        value[key] = item
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _decode(value, token):
    if not isinstance(value, dict) or set(value) != set(IndexRefreshLease.__dataclass_fields__):
        raise ValueError('Invalid index-refresh lease schema')
    if type(value['version']) is not int or value['version'] != 1 or value['token'] != _token(token):
        raise ValueError('Invalid index-refresh lease identity/version')
    _index(value['index'])
    for key in ('scope_identity', 'config_identity', 'owner_identity'):
        item = value[key]
        if not isinstance(item, str) or (not item and key != 'config_identity') or (item and not re.fullmatch(r'[a-f0-9]{64}', item)):
            raise ValueError('Invalid index-refresh configuration identity')
    created, expires = value['created_at'], value['expires_at']
    if not _number(created) or not _number(expires) or created < 0 or expires - created != TTL_SECONDS:
        raise ValueError('Invalid index-refresh lease lifetime')
    captures = value['captured']
    if not isinstance(captures, list) or len(captures) > MAX_ROOTS:
        raise ValueError('Invalid index-refresh captured roots')
    roots, parsed, reference_count = set(), [], 0
    for item in captures:
        if not isinstance(item, dict) or set(item) != {'root', 'generation', 'references'}:
            raise ValueError('Invalid captured generation')
        root, generation = item['root'], item['generation']
        if (not isinstance(root, str) or len(root.encode()) > 4096 or any(ord(c) < 32 for c in root)
                or not Path(root).is_absolute()
                or str(Path(root)) != root or '..' in Path(root).parts or root in roots
                or type(generation) is not int or not 0 <= generation < 2 ** 63):
            raise ValueError('Invalid captured generation')
        roots.add(root)
        references = item['references']
        if not isinstance(references, list):
            raise ValueError('Invalid captured index references')
        reference_count += len(references)
        if reference_count > MAX_REFERENCES:
            raise ValueError('Too many captured index references')
        seen, refs = set(), []
        for ref in references:
            if not isinstance(ref, dict) or set(ref) != {'collection', 'path', 'revision'}:
                raise ValueError('Invalid indexed revision reference')
            collection, relative, revision = (ref[key] for key in ('collection', 'path', 'revision'))
            if (not isinstance(collection, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', collection)
                    or not isinstance(relative, str) or not relative or len(relative.encode()) > 4096
                    or any(ord(c) < 32 for c in relative) or Path(relative).is_absolute()
                    or str(Path(relative)) != relative or '..' in Path(relative).parts
                    or relative in seen or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision)):
                raise ValueError('Invalid indexed revision reference')
            seen.add(relative)
            refs.append(IndexedRevision(collection, relative, revision))
        parsed.append(CapturedGeneration(root, generation, tuple(refs)))
    warning = value['warning']
    if not isinstance(warning, str) or len(warning) > 512 or (parsed and (warning or not value['config_identity'])):
        raise ValueError('Invalid index-refresh binding/warning')
    state, lexical, vector, completed = (value[key] for key in ('state', 'lexical_ok', 'vector_ok', 'completed_at'))
    proofs = value['proofs']
    if not isinstance(proofs, list) or len(proofs) > MAX_ROOTS:
        raise ValueError('Invalid index completion proofs')
    verified, proved_roots = [], set()
    for proof in proofs:
        if not isinstance(proof, dict) or set(proof) != {'root', 'lexical', 'vector', 'reason'}:
            raise ValueError('Invalid index completion proof')
        root, text_ok, vectors_ok, reason = (proof[key] for key in ('root', 'lexical', 'vector', 'reason'))
        if (not isinstance(root, str) or root not in roots or root in proved_roots or type(text_ok) is not bool or type(vectors_ok) is not bool
                or (vectors_ok and not text_ok) or not isinstance(reason, str) or len(reason) > 512):
            raise ValueError('Invalid index completion proof')
        proved_roots.add(root)
        verified.append(VerifiedStages(root, text_ok, vectors_ok, reason))
    if state == 'open':
        if lexical is not None or vector is not None or completed is not None or proofs:
            raise ValueError('Open lease cannot contain completion flags')
    elif state in {'finishing', 'complete'}:
        if type(lexical) is not bool or type(vector) is not bool or (vector and not lexical):
            raise ValueError('Invalid index-refresh completion flags')
        if proved_roots != roots or any((p.lexical and not lexical) or (p.vector and not vector) for p in verified):
            raise ValueError('Completion proof exceeds the reported stages')
        if state == 'finishing' and completed is not None:
            raise ValueError('Unfinished lease has a completion timestamp')
        if state == 'complete' and (not _number(completed) or not created <= completed < expires):
            raise ValueError('Invalid index-refresh completion timestamp')
    else:
        raise ValueError('Invalid index-refresh lease state')
    return IndexRefreshLease(**{**value, 'captured': tuple(parsed), 'proofs': tuple(verified)})


def _read(token):
    path = _path(token)
    with state_io._parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                    or info.st_size > MAX_LEASE_BYTES):
                raise ValueError('Lease must be a private bounded singly linked regular file')
            body = handle.read(MAX_LEASE_BYTES + 1)
    if len(body) > MAX_LEASE_BYTES:
        raise ValueError('Index-refresh lease exceeds its bound')
    try:
        value = json.loads(body, object_pairs_hook=_unique)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid index-refresh lease encoding/depth') from exc
    return _decode(value, token)


def _body(lease):
    body = json.dumps(asdict(lease), sort_keys=True).encode()
    if len(body) > MAX_LEASE_BYTES:
        raise ValueError('Index-refresh lease exceeds its bound')
    _decode(json.loads(body), lease.token)
    return body


@contextmanager
def _lock(token):
    descriptor = state_io.open_lock(_path(token).with_suffix('.lock'))
    deadline = time.monotonic() + LOCK_TIMEOUT
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Index-refresh lease lock deadline')
                time.sleep(.01)
        yield
    finally:
        os.close(descriptor)


def begin(index=''):
    """Capture metadata after the caller acquires its real indexing lock."""
    index = _index(index)
    scope, warning = _scope(index)
    if index != setup_recall.recall_mod.Settings.load().index:
        warning = 'Only the configured canonical recall index may acknowledge freshness; no freshness will be acknowledged.'
    captured, configuration = ({}, '') if warning else setup_recall._freshness_capture(index)
    if not captured and not warning:
        warning = ('QMD configuration is missing or changed; no freshness will be acknowledged.' if not configuration else
                   'No eligible full-root collection journals were captured; no freshness will be acknowledged.')
    if _scope(index)[0] != scope:
        captured, configuration = {}, ''
        warning = 'QMD scope changed during capture; no freshness will be acknowledged.'
    owner = owner_identity(index)
    generations = []
    collections = setup_recall._freshness_collections(index)[0] if captured else {}
    for root, generation in sorted(captured.items()):
        current = freshness.snapshot(root)
        if current.get('index_owner') not in (None, owner):
            warning = 'A captured journal belongs to another index; no freshness will be acknowledged.'
            generations = []
            break
        aliases = sorted(name for name, path in collections.items() if path.expanduser().resolve() == root
                         and re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', name))
        references = []
        retained = current.get('retained_revisions', current['dirty'])
        for relative, entry in sorted(retained.items()):
            if entry.get('generation', generation + 1) > generation:
                continue
            if entry.get('status') != 'saved' or not re.fullmatch(r'[a-f0-9]{64}', str(entry.get('revision', ''))):
                warning = 'A captured journal has an unresolved mutation; no freshness will be acknowledged.'
                break
            if aliases:
                references.append(IndexedRevision(aliases[0], relative, entry['revision']))
        if warning:
            generations = []
            break
        if not aliases or (generation and not references and current.get('index_owner') != owner):
            warning = 'Captured revisions cannot establish index ownership; no freshness will be acknowledged.'
            generations = []
            break
        generations.append(CapturedGeneration(str(root), generation, tuple(references)))
    if _scope(index)[0] != scope or (configuration and setup_recall._freshness_config(index)[1] != configuration):
        generations, configuration = [], ''
        warning = 'QMD scope changed during capture; no freshness will be acknowledged.'
    now, token = time.time(), str(uuid.uuid4())
    lease = IndexRefreshLease(1, token, index, scope, configuration, owner, now, now + TTL_SECONDS,
                             tuple(generations), warning)
    body = _body(lease)
    path = _path(token)
    if path.exists() or path.is_symlink():
        raise FileExistsError('Index-refresh lease identity already exists')
    state_io.create_bytes(path, body)
    return {'token': token, 'status': 'captured' if generations else 'unbound',
            'captured_roots': len(generations), 'warning': warning}


def _result(lease, replayed):
    bound = bool(lease.captured)
    text_count = sum(proof.lexical for proof in lease.proofs)
    vector_count = sum(proof.vector for proof in lease.proofs)
    status = 'unbound' if not bound else 'acknowledged' if text_count else 'no_successful_stages'
    warnings = [lease.warning] + sorted({proof.reason for proof in lease.proofs if proof.reason})
    return {'token': lease.token, 'status': status, 'replayed': replayed,
            'acknowledged': {'lexical': text_count, 'vector': vector_count},
            'warning': '; '.join(w for w in warnings if w)[:1024],
            'evidence': 'reported stages plus read-only indexed revision proof; tracked mutations only'}


def _validate_current(lease):
    now = time.time()
    if now < lease.created_at or now >= lease.expires_at:
        raise ValueError('Index-refresh lease is expired or its clock moved backwards')
    if _scope(lease.index)[0] != lease.scope_identity or owner_identity(lease.index) != lease.owner_identity:
        raise ValueError('QMD scope changed since index-refresh begin')
    if lease.captured and setup_recall.recall_mod.Settings.load().index != lease.index:
        raise ValueError('Canonical recall index changed since index-refresh begin')
    if lease.config_identity and setup_recall._freshness_config(lease.index)[1] != lease.config_identity:
        raise ValueError('QMD configuration changed since index-refresh begin')
    now = time.time()
    if now < lease.created_at or now >= lease.expires_at:
        raise ValueError('Index-refresh lease is expired or its clock moved backwards')
    return now


def finish(token, lexical_ok=False, vector_ok=False):
    """Acknowledge captured generations only for the caller's successful stages."""
    _token(token)
    if type(lexical_ok) is not bool or type(vector_ok) is not bool or (vector_ok and not lexical_ok):
        raise ValueError('Vector success requires lexical success; stage flags must be booleans')
    with _lock(token):
        lease = _read(token)
        _validate_current(lease)
        if lease.state != 'open' and (lease.lexical_ok != lexical_ok or lease.vector_ok != vector_ok):
            raise ValueError('Index-refresh completion flags differ from the saved attempt')
        if lease.state == 'complete':
            return _result(lease, True)
        if lease.state == 'open':
            from . import index_proof
            database, configuration = _paths(lease.index)
            qmd = setup_recall.qmd_exe() if lexical_ok and lease.captured else ''
            references = tuple(asdict(ref) for captured in lease.captured for ref in captured.references)
            result = index_proof.verify(database=str(database), configuration=str(configuration), qmd=qmd or '',
                references=references, vector=vector_ok) if lexical_ok and lease.captured else {
                    'lexical': False, 'vector': False, 'reason': ''}
            if (type(result.get('lexical')) is not bool or type(result.get('vector')) is not bool
                    or (result['vector'] and not result['lexical'])):
                raise ValueError('Invalid read-only index proof result')
            proofs = [VerifiedStages(captured.root, result['lexical'], result['vector'] and vector_ok,
                                     str(result.get('reason', ''))[:512]) for captured in lease.captured]
            _validate_current(lease)
            lease = replace(lease, state='finishing', lexical_ok=lexical_ok, vector_ok=vector_ok, proofs=tuple(proofs))
            state_io.atomic_bytes(_path(token), _body(lease))
        proofs_by_root = {proof.root: proof for proof in lease.proofs}
        for captured in lease.captured:
            proof = proofs_by_root[captured.root]
            if proof.lexical:
                _validate_current(lease)
                freshness.acknowledge(captured.generation, capability='lexical', vault_root=Path(captured.root),
                                      index_owner=lease.owner_identity, deadline=lease.expires_at,
                                      proof_scope=_proof_identity(lease.scope_identity, lease.config_identity))
            if proof.vector:
                _validate_current(lease)
                freshness.acknowledge(captured.generation, capability='vector', vault_root=Path(captured.root),
                                      index_owner=lease.owner_identity, deadline=lease.expires_at,
                                      proof_scope=_proof_identity(lease.scope_identity, lease.config_identity))
        lease = replace(lease, state='complete', completed_at=_validate_current(lease))
        state_io.atomic_bytes(_path(token), _body(lease))
        return _result(lease, False)


def add_parser(sub):
    parser = sub.add_parser('index-refresh', help='Metadata-only begin/finish hooks for cooperating index wrappers')
    commands = parser.add_subparsers(dest='refresh_action', required=True)
    start = commands.add_parser('begin')
    start.add_argument('--index', default='', help='Actual QMD --index name; omit for the default index')
    end = commands.add_parser('finish')
    end.add_argument('--token', required=True)
    end.add_argument('--lexical-ok', action='store_true')
    end.add_argument('--vector-ok', action='store_true')


def main(args):
    try:
        result = begin(args.index) if args.refresh_action == 'begin' else finish(
            args.token, lexical_ok=args.lexical_ok, vector_ok=args.vector_ok)
        if result['warning']:
            print('index-refresh: ' + result['warning'], file=sys.stderr)
        print(result['token'] if args.refresh_action == 'begin' else json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f'index-refresh failed: {exc}', file=sys.stderr)
        return 1
