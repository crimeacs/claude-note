"""Durable local import publication; receipts are evidence, never authority grants.

Each publication owns immutable source bytes and a complete two-event batch.
The ordinary hook append journals remain unchanged. Canonical receipts and
evidence outlive queue cleanup; this is not exactly-once model execution.
"""

import fcntl
import hashlib
import io
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import config, models, state_io

KEY = "_import_publication"
REVISION_KEY = "_import_revision"
MAX_RAW = 256 * 1024 * 1024
MAX_RECORD = 1024 * 1024
MAX_BATCH = 2 * MAX_RECORD
HEX = re.compile(r"[0-9a-f]{64}\Z")


class PublicationError(ValueError):
    def __init__(self, message, session_id=""):
        super().__init__(message)
        self.session_id = session_id


@dataclass(frozen=True)
class PublicationResult:
    publication_id: str
    status: str
    snapshot_path: Path
    captured_revision: str
    logical_revision: str
    queue_path: Path
    event_ids: tuple
    ordinal: int


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PublicationError("Duplicate publication field")
        value[key] = item
    return value


def _read(path, limit):
    with state_io._parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink not in (1, 2) or before.st_size > limit:
                raise PublicationError("Invalid bounded publication file")
            raw = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
            if len(raw) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise PublicationError("Publication file changed during inspection")
        if before.st_nlink == 2:
            # Recover only state_io's exact intent-bound interrupted hardlink.
            state_io.create_bytes(path, raw)
        return raw


def _record(path):
    raw = _read(path, MAX_BATCH)
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PublicationError("Invalid publication receipt") from exc
    if not isinstance(value, dict):
        raise PublicationError("Invalid publication receipt")
    return value


def _receipt(publication_id):
    if not isinstance(publication_id, str) or not HEX.fullmatch(publication_id):
        raise PublicationError("Invalid publication identity")
    return config.STATE_DIR / "import-publications" / (publication_id + ".json")


def _intent(publication_id):
    _receipt(publication_id)
    return config.STATE_DIR / "import-intents" / (publication_id + ".json")


def batch_path(publication_id):
    _receipt(publication_id)
    return config.QUEUE_DIR / ("import-" + publication_id + ".jsonl")


def is_batch(path):
    return Path(path).name.startswith("import-")


@contextmanager
def _stream_lock(stream):
    descriptor = state_io.open_lock(config.STATE_DIR / "import-streams" / (stream + ".lock"))
    deadline = time.monotonic() + 5
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Import publication lock deadline")
                time.sleep(.01)
        yield
    finally:
        os.close(descriptor)


def _equivalent(first, second):
    """Only normalized origin metadata may differ under a logical revision."""
    first_lines, second_lines = io.BytesIO(first), io.BytesIO(second)
    while True:
        left, right = first_lines.readline(8 * MAX_RECORD + 1), second_lines.readline(8 * MAX_RECORD + 1)
        if not left or not right:
            return left == right
        if len(left) > 8 * MAX_RECORD or len(right) > 8 * MAX_RECORD:
            return False
        try:
            a, b = (json.loads(raw, object_pairs_hook=_unique) for raw in (left, right))
            if not isinstance(a, dict) or not isinstance(b, dict):
                return False
            for record in (a, b):
                origins = record.pop("derivation_sources", None)
                if not isinstance(origins, list) or not all(isinstance(p, str) for p in origins):
                    return False
            # Python structural equality conflates True/1 and 1/1.0. Compare
            # normalized JSON bytes to retain exact scalar types as evidence.
            if _json(a) != _json(b):
                return False
        except (ValueError, UnicodeError):
            return False


def _validate(record, publication_id):
    session = record.get("session_id", "")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session):
        session = ""
    expected = {"version", "publication_id", "source", "session_id", "logical_id", "logical_revision", "captured_revision", "normalized", "transcript_path", "snapshot_path", "stream", "ordinal", "events"}
    if set(record) != expected or type(record.get("version")) is not int or record["version"] != 1:
        raise PublicationError("Unsupported publication receipt", session)
    try:
        if record["publication_id"] != publication_id or not isinstance(record["normalized"], bool):
            raise ValueError()
        for field in ("source", "session_id", "logical_id", "transcript_path"):
            if not isinstance(record[field], str) or not record[field] or len(record[field]) > 8192:
                raise ValueError()
        for field in ("logical_revision", "captured_revision", "stream"):
            if not isinstance(record[field], str) or not HEX.fullmatch(record[field]):
                raise ValueError()
        if not record["normalized"] and record["logical_revision"] != record["captured_revision"]:
            raise ValueError()
        identity = [record["source"], session, record["logical_id"], record["logical_revision"]]
        if _hash(_json(identity)) != publication_id or _hash(_json([record["source"], session])) != record["stream"]:
            raise ValueError()
        if type(record["ordinal"]) is not int or not 1 <= record["ordinal"] <= 2**53:
            raise ValueError()
        snapshot = config.STATE_DIR / "sources" / (record["captured_revision"] + ".jsonl")
        if record["snapshot_path"] != str(snapshot.absolute()) or not Path(record["transcript_path"]).is_absolute():
            raise ValueError()
        if not isinstance(record["events"], list) or len(record["events"]) != 2:
            raise ValueError()
        events = [models.QueuedEvent(**event).validate() for event in record["events"]]
        for event, kind in zip(events, ("UserPromptSubmit", "Stop")):
            if (event.session_id != session or event.event != kind or event.transcript_path != record["transcript_path"]
                    or event.event_id != _hash(_json([publication_id, kind]))
                    or event.data.get(KEY) != publication_id
                    or event.data.get(REVISION_KEY) != record["captured_revision"]):
                raise ValueError()
        if events[0].ts >= events[1].ts or events[0].cwd != events[1].cwd:
            raise ValueError()
        if len(_batch_bytes(record)) > MAX_BATCH:
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise PublicationError("Invalid publication receipt binding", session) from exc
    return record


def _batch_bytes(record):
    return b"".join(_json(event) + b"\n" for event in record["events"])


def load(publication_id, *, verify_source=True):
    record = _validate(_record(_receipt(publication_id)), publication_id)
    if verify_source:
        try:
            raw = _read(Path(record["snapshot_path"]), MAX_RAW)
            if _hash(raw) != record["captured_revision"]:
                raise ValueError()
        except (OSError, ValueError) as exc:
            raise PublicationError("Imported evidence missing or changed", record["session_id"]) from exc
    return record


def validate_event(event):
    """A caller-supplied binding never grants authority; require canonical bytes."""
    if KEY not in event.data:
        return None
    try:
        record = load(event.data[KEY])
        if json.loads(event.to_json()) not in record["events"]:
            raise PublicationError("Imported event differs from canonical publication", event.session_id)
        return record
    except (OSError, ValueError, TypeError) as exc:
        raise PublicationError("Imported event could not be verified", event.session_id) from exc


def _validate_batch(path, *, verify_source):
    name = Path(path).name
    if not re.fullmatch(r"import-[0-9a-f]{64}\.jsonl", name):
        raise PublicationError("Invalid import batch name")
    record = load(name[7:-6], verify_source=verify_source)
    try:
        if _read(path, MAX_BATCH) != _batch_bytes(record):
            raise ValueError()
    except (OSError, ValueError) as exc:
        raise PublicationError("Import batch differs from canonical publication", record["session_id"]) from exc
    return record


def validate_batch(path):
    """Verify canonical metadata, exact delivery bytes and current source digest."""
    return _validate_batch(path, verify_source=True)


def _validate_batch_metadata(path):
    """Inspect delivery metadata only; this does not verify source evidence.

    Queue scheduling may use this to decide which sessions need work. Delivery,
    acknowledgement and source use still require their full verification gate.
    """
    return _validate_batch(path, verify_source=False)


def batch_session_hints(path):
    """Untrusted hints may restrict delivery, never authorize capture or reads."""
    sessions = set()
    try:
        raw = _read(path, MAX_BATCH)
        for line in raw.splitlines():
            try:
                event = json.loads(line, object_pairs_hook=_unique)
                session = event.get("session_id") if isinstance(event, dict) else None
                if isinstance(session, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session):
                    sessions.add(session)
            except (ValueError, UnicodeError, RecursionError):
                continue
    except (OSError, ValueError):
        pass
    return sessions


def binding(record):
    return {key: record[key] for key in ("publication_id", "source", "logical_revision", "captured_revision", "snapshot_path", "transcript_path", "stream", "ordinal")}


def read_binding(state):
    declared = state.import_binding
    if not isinstance(declared, dict):
        raise PublicationError("Invalid session import binding", state.session_id)
    record = load(declared.get("publication_id"), verify_source=False)
    if (declared != binding(record) or record["session_id"] != state.session_id
            or record["transcript_path"] != state.transcript_path):
        raise PublicationError("Session import binding differs from canonical receipt", state.session_id)
    raw = _read(Path(record["snapshot_path"]), MAX_RAW)
    if _hash(raw) != record["captured_revision"]:
        raise PublicationError("Imported evidence missing or changed", state.session_id)
    return record, raw


def has_history(source, session_id):
    stream = _hash(_json([source, session_id]))
    return (config.STATE_DIR / "import-streams" / (stream + ".json")).exists()


def protect_existing(source, session_id, logical_id, raw, origin_sources=(), *, logical_revision):
    """Intersect a rediscovered origin without publishing legacy/seen work."""
    if not isinstance(logical_revision, str) or not HEX.fullmatch(logical_revision):
        raise PublicationError("Invalid logical import revision")
    publication_id = _hash(_json([source, session_id, logical_id, logical_revision]))
    stream = _hash(_json([source, session_id]))
    with _stream_lock(stream):
        try:
            record = load(publication_id)
        except FileNotFoundError:
            return False
        canonical = _read(Path(record["snapshot_path"]), MAX_RAW)
        if (not isinstance(raw, bytes) or len(raw) > MAX_RAW or not raw.endswith(b"\n")
                or not record["normalized"] or (raw != canonical and not _equivalent(canonical, raw))):
            raise PublicationError("Rediscovered import identity/content conflict", session_id)
        origins = sorted({record["transcript_path"], *(str(Path(p).absolute()) for p in origin_sources)})
        if len(origins) > 256 or any(len(p) > 8192 for p in origins):
            raise PublicationError("Import origin closure exceeds bounds")
        from . import egress
        egress.capture_restrictions(Path(record["snapshot_path"]), origins=origins,
                                    captured_text=canonical.decode("utf-8"), vault_root=config.STATE_DIR)
        return True


def publish(source, session_id, logical_id, transcript_path, raw, cwd, prompt,
            origin_sources=(), *, logical_revision=None):
    """Persist evidence/protection/first-attempt pair before atomic visibility."""
    path = str(Path(transcript_path).absolute())
    if (not isinstance(raw, bytes) or not raw or len(raw) > MAX_RAW or not raw.endswith(b"\n")
            or any(not isinstance(value, str) or not value or len(value) > 8192 for value in (source, session_id, logical_id, path))
            or not isinstance(cwd, str) or len(cwd) > 8192 or not isinstance(prompt, str) or len(prompt.encode()) > 256 * 1024):
        raise PublicationError("Invalid bounded import publication input")
    text = raw.decode("utf-8", errors="strict")
    normalized = logical_revision is not None
    revision = logical_revision if normalized else _hash(raw)
    if not isinstance(revision, str) or not HEX.fullmatch(revision):
        raise PublicationError("Invalid logical import revision")
    origins = sorted({path, *(str(Path(origin).absolute()) for origin in origin_sources)})
    if len(origins) > 256 or any(len(origin) > 8192 for origin in origins):
        raise PublicationError("Import origin closure exceeds bounds")
    publication_id = _hash(_json([source, session_id, logical_id, revision]))
    stream = _hash(_json([source, session_id]))
    receipt_path, queue_path = _receipt(publication_id), batch_path(publication_id)
    with _stream_lock(stream):
        try:
            try:
                record = load(publication_id)
            except FileNotFoundError:
                record = _validate(_record(_intent(publication_id)), publication_id)
            try:
                existing = _read(Path(record["snapshot_path"]), MAX_RAW)
            except FileNotFoundError as exc:
                raise PublicationError("Existing import intent has missing evidence; preserve for recovery", session_id) from exc
            if _hash(existing) != record["captured_revision"]:
                raise PublicationError("Imported evidence missing or changed", session_id)
            if (record["transcript_path"] != path or record["normalized"] != normalized
                    or record["events"][0]["cwd"] != cwd
                    or record["events"][0]["data"].get("prompt") != prompt
                    or (existing != raw and (not normalized or not _equivalent(existing, raw)))):
                raise PublicationError("Import replay identity/content conflict", session_id)
            text = existing.decode("utf-8")
        except FileNotFoundError:
            stream_path = config.STATE_DIR / "import-streams" / (stream + ".json")
            try:
                counter = state_io.read_json(stream_path)
                if set(counter) != {"version", "ordinal"} or counter["version"] != 1 or type(counter["ordinal"]) is not int or not 0 <= counter["ordinal"] < 2**53:
                    raise PublicationError("Invalid import stream counter")
                ordinal = counter["ordinal"] + 1
            except FileNotFoundError:
                ordinal = 1
            state_io.atomic_json(stream_path, {"version": 1, "ordinal": ordinal})
            captured = _hash(raw)
            snapshot = config.STATE_DIR / "sources" / (captured + ".jsonl")
            state_io.create_bytes(snapshot, raw)
            now = datetime.utcnow()
            events = []
            for i, kind in enumerate(("UserPromptSubmit", "Stop")):
                data = {"hook_event_name": kind, "session_id": session_id, "transcript_path": path, "cwd": cwd,
                        "prompt": prompt if i == 0 else "", "import_source": source, KEY: publication_id,
                        REVISION_KEY: captured}
                event = models.QueuedEvent(_hash(_json([publication_id, kind])),
                                          (now + timedelta(microseconds=i)).isoformat(timespec="microseconds") + "Z",
                                          kind, session_id, cwd, path, data).validate()
                events.append(json.loads(event.to_json()))
            record = {"version": 1, "publication_id": publication_id, "source": source, "session_id": session_id,
                      "logical_id": logical_id, "logical_revision": revision, "captured_revision": captured,
                      "normalized": normalized, "transcript_path": path, "snapshot_path": str(snapshot.absolute()),
                      "stream": stream, "ordinal": ordinal, "events": events}
            _validate(record, publication_id)
        # This immutable intent preserves the first durable timestamps/pair even
        # if protection inspection or queue publication subsequently fails.
        state_io.create_bytes(_intent(publication_id), _json(record))
        from . import egress
        # Authored controls + protected lineage become durable before visibility.
        # Current configurable path policy remains live and is never granted here.
        egress.capture_restrictions(Path(record["snapshot_path"]), origins=origins,
                                    captured_text=text, vault_root=config.STATE_DIR)
        state_io.create_bytes(receipt_path, _json(record))
        from .session_tracker import load_session_state
        state = load_session_state(session_id)
        ids = tuple(event["event_id"] for event in record["events"])
        captured = bool(state and all(event_id in state.processed_event_ids for event_id in ids))
        visible = queue_path.exists()
        if not captured or visible:
            state_io.create_bytes(queue_path, _batch_bytes(record))
        return PublicationResult(publication_id, "captured" if captured else "replayed" if visible else "published",
                                 Path(record["snapshot_path"]), record["captured_revision"], revision,
                                 queue_path, ids, record["ordinal"])
