"""Queue management for claude-note events."""

import fcntl
import os
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from . import config
from . import models
from .state_io import append_line, atomic_json, read_json


MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_PENDING_BYTES = 16 * 1024 * 1024


def get_queue_file(date: Optional[datetime] = None) -> Path:
    """Get the queue file path for a given date (default: today)."""
    if date is None:
        date = datetime.utcnow()
    filename = date.strftime("%Y-%m-%d") + ".jsonl"
    return config.QUEUE_DIR / filename


def enqueue_event(event: models.QueuedEvent) -> None:
    """Append an event to the queue file (atomic, with file locking)."""
    queue_file = get_queue_file()
    queue_file.parent.mkdir(parents=True, exist_ok=True)

    json_line = event.to_json() + "\n"

    append_line(queue_file, json_line.encode())


def read_queue_files() -> Iterator[Path]:
    """Iterate over all queue files in chronological order."""
    if not config.QUEUE_DIR.exists():
        return

    files = sorted(config.QUEUE_DIR.glob("*.jsonl"))
    yield from files


def read_events(queue_file: Path) -> Iterator[models.QueuedEvent]:
    """Read all events from a queue file."""
    if not queue_file.exists():
        return

    from . import import_publication
    if import_publication.is_batch(queue_file):
        record = import_publication.validate_batch(queue_file)
        yield from (models.QueuedEvent(**event).validate() for event in record["events"])
        return

    with open(queue_file, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield models.QueuedEvent.from_json(line)
                except Exception:
                    # Skip malformed lines
                    continue


def read_all_events() -> Iterator[models.QueuedEvent]:
    """Read all events from all queue files."""
    for queue_file in read_queue_files():
        yield from read_events(queue_file)


def read_pending_events(limit: int = 10000, *, max_bytes: int = MAX_PENDING_BYTES) -> Iterator[models.QueuedEvent]:
    """Advance append-journal offsets only past durably captured session events.

    Delivery itself does not acknowledge a record. Prefix digests detect edits
    and truncation, with full replay on a mismatch. A partial final line waits
    for the append to complete. Session-state dedup remains the effect boundary.
    Canonical import metadata is checked globally. Before delivery or a journal
    acknowledgement, all visible import snapshots for that session are hashed.
    Idle captured batches need no snapshot reads; metadata work still scales
    with the visible backlog. Source use/capture has its own current checks.

    One delivery is bounded by both count and persisted UTF-8 record bytes,
    including each newline. The byte budget must fit one maximum-size journal
    record so a valid event can always make progress in an empty delivery.
    This bounds the returned payload, not Python object overhead or the memory
    needed to verify retained import metadata and source evidence.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("Pending event count limit must be a nonnegative integer")
    if type(max_bytes) is not int or max_bytes < MAX_EVENT_BYTES:
        raise ValueError("Pending byte budget must fit one maximum-size event")
    if not limit:
        return
    from .session_tracker import load_session_state
    state_cache = {}
    delivered, delivered_bytes = 0, 0
    from . import import_publication
    paths = list(read_queue_files())
    batches, session_batches, blocked_sessions = {}, {}, set()
    # Check ALL visible canonical metadata, including already captured batches.
    # This first pass does not attest any snapshot's current content digest.
    for path in paths:
        if import_publication.is_batch(path):
            try:
                record = import_publication._validate_batch_metadata(path)
                batches[path] = record
                session_batches.setdefault(record["session_id"], []).append((path, record))
            except (OSError, ValueError) as exc:
                session = getattr(exc, "session_id", "")
                if session:
                    blocked_sessions.add(session)
                blocked_sessions.update(import_publication.batch_session_hints(path))
                logging.getLogger(__name__).warning("Import batch verification failed (%s); evidence retained", path.name[:80])

    verified_sessions = set()

    def verify_session(session_id):
        if session_id in blocked_sessions:
            return False
        if session_id in verified_sessions:
            return True
        # Include captured historical revisions: a corrupt older visible source
        # must still block newer events, ordinary hooks and journal cursor ACKs.
        # Verification is scoped to this invocation, never cached by file stat.
        for path, expected in session_batches.get(session_id, ()):
            try:
                current = import_publication.validate_batch(path)
                if import_publication._json(current) != import_publication._json(expected):
                    raise import_publication.PublicationError("Import metadata changed during preflight", session_id)
            except (OSError, ValueError):
                blocked_sessions.add(session_id)
                logging.getLogger(__name__).warning("Import session verification failed (%s); evidence retained", path.name[:80])
                return False
        verified_sessions.add(session_id)
        return True

    for path in paths:
        if import_publication.is_batch(path):
            record = batches.get(path)
            if not record or record["session_id"] in blocked_sessions:
                continue
            try:
                state = load_session_state(record["session_id"])
            except (OSError, ValueError):
                blocked_sessions.add(record["session_id"])
                logging.getLogger(__name__).warning("Incompatible import session state retained; skipping this session")
                continue
            captured = set(state.processed_event_ids) if state else set()
            if (any(payload["event_id"] not in captured for payload in record["events"])
                    and not verify_session(record["session_id"])):
                continue
            for payload in record["events"]:
                event = models.QueuedEvent(**payload).validate()
                if event.event_id not in captured:
                    size = len(import_publication._json(payload)) + 1
                    if delivered_bytes + size > max_bytes:
                        return
                    delivered += 1
                    delivered_bytes += size
                    yield event
                    if delivered >= limit:
                        return
            continue
        cursor = config.STATE_DIR / "queue-offsets" / (path.stem + ".json")
        try:
            saved = read_json(cursor)
        except (OSError, ValueError):
            saved = {}
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            offset = saved.get("offset", 0)
            digest = hashlib.sha256()
            if not isinstance(offset, int) or offset < 0 or offset > info.st_size:
                offset = 0
            remaining = offset
            while remaining:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining or (offset and digest.hexdigest() != saved.get("prefix_revision")):
                handle.seek(0)
                offset, digest = 0, hashlib.sha256()
            acknowledged, contiguous = offset, True
            acknowledged_digest = digest.hexdigest()
            budget_exhausted = False
            while True:
                line = handle.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT_BYTES or not line.endswith(b"\n"):
                    break  # incomplete/oversized evidence is never acknowledged
                digest.update(line)
                try:
                    event = models.QueuedEvent.from_json(line)
                except (ValueError, TypeError):
                    declares_import = False
                    try:
                        malformed = json.loads(line)
                        payload = malformed.get("data") if isinstance(malformed, dict) else None
                        declares_import = isinstance(payload, dict) and any(
                            key in payload for key in (import_publication.KEY, import_publication.REVISION_KEY))
                    except (ValueError, UnicodeError):
                        pass
                    if declares_import or b"_import_publication" in line or b"_import_revision" in line:
                        contiguous = False
                        logging.getLogger(__name__).warning("Malformed imported journal reference retained without acknowledgement")
                        continue
                    # Invalid full records remain inspectable in the journal.
                    if contiguous:
                        acknowledged, acknowledged_digest = handle.tell(), digest.hexdigest()
                    continue
                if not verify_session(event.session_id):
                    contiguous = False
                    continue
                # Imported references copied into an ordinary hook journal
                # remain imports; invalid references never inherit malformed-
                # legacy-line cursor acknowledgement.
                if import_publication.KEY in event.data:
                    try:
                        import_publication.validate_event(event)
                    except (OSError, ValueError):
                        contiguous = False
                        logging.getLogger(__name__).warning("Imported journal reference could not be verified; evidence retained")
                        continue
                if event.session_id not in state_cache:
                    try:
                        state = load_session_state(event.session_id)
                    except (OSError, ValueError):
                        blocked_sessions.add(event.session_id)
                        contiguous = False
                        logging.getLogger(__name__).warning("Incompatible journal session state retained; skipping this session")
                        continue
                    state_cache[event.session_id] = set(state.processed_event_ids) if state else set()
                captured = event.event_id in state_cache[event.session_id]
                if contiguous and captured:
                    acknowledged, acknowledged_digest = handle.tell(), digest.hexdigest()
                elif not captured:
                    contiguous = False
                    if delivered_bytes + len(line) > max_bytes:
                        budget_exhausted = True
                        break
                    delivered += 1
                    delivered_bytes += len(line)
                    yield event
                    if delivered >= limit:
                        break
            if acknowledged > offset:
                atomic_json(cursor, {"version": 1, "offset": acknowledged, "prefix_revision": acknowledged_digest})
        if delivered >= limit or budget_exhausted:
            return


def get_events_by_session(session_id: str) -> list[models.QueuedEvent]:
    """Get all events for a specific session."""
    events = []
    for event in read_all_events():
        if event.session_id == session_id:
            events.append(event)
    return events


def get_unprocessed_sessions(processed_sessions: set) -> dict[str, list]:
    """
    Get sessions with unprocessed events.

    Args:
        processed_sessions: Set of session_ids that have been fully processed

    Returns:
        Dict mapping session_id to list of events
    """
    sessions: dict = {}

    for event in read_all_events():
        if event.session_id not in processed_sessions:
            if event.session_id not in sessions:
                sessions[event.session_id] = []
            sessions[event.session_id].append(event)

    return sessions


def cleanup_old_queue_files(keep_days: int = 7) -> None:
    """Remove queue files older than keep_days."""
    if not config.QUEUE_DIR.exists():
        return

    cutoff = datetime.utcnow().date()
    for queue_file in config.QUEUE_DIR.glob("*.jsonl"):
        from . import import_publication
        if import_publication.is_batch(queue_file):
            try:
                record = import_publication._validate_batch_metadata(queue_file)
                created = datetime.fromisoformat(record["events"][0]["ts"].rstrip("Z")).date()
                if (cutoff - created).days <= keep_days:
                    continue
                from .session_tracker import load_session_state
                state = load_session_state(record["session_id"])
                if state and all(event["event_id"] in state.processed_event_ids for event in record["events"]):
                    # Age/state only select deletion candidates. Recheck exact
                    # metadata and source bytes before retiring a delivery copy.
                    current = import_publication.validate_batch(queue_file)
                    if import_publication._json(current) != import_publication._json(record):
                        continue
                    state = load_session_state(record["session_id"])
                    if not state or not all(event["event_id"] in state.processed_event_ids for event in record["events"]):
                        continue
                    # Canonical publication receipt, protected lineage and source
                    # snapshots are retained. Replay sees captured IDs and does
                    # not resurrect a cleaned batch.
                    queue_file.unlink()
            except (OSError, ValueError, TypeError):
                pass
            continue
        try:
            file_date = datetime.strptime(queue_file.stem, "%Y-%m-%d").date()
            age_days = (cutoff - file_date).days
            if age_days > keep_days:
                from .session_tracker import load_session_state
                captured = True
                try:
                    raw = queue_file.read_bytes()
                    for line in raw.splitlines(keepends=True):
                        if not line.endswith(b"\n"):
                            raise ValueError("Partial queue record")
                        models.QueuedEvent.from_json(line)
                except (OSError, ValueError, TypeError):
                    continue
                for event in read_events(queue_file):
                    state = load_session_state(event.session_id)
                    if not state or event.event_id not in state.processed_event_ids:
                        captured = False
                        break
                if captured:
                    queue_file.unlink()
        except ValueError:
            # Skip files that don't match the date pattern
            continue
