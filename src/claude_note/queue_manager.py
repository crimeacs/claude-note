"""Queue management for claude-note events."""

import fcntl
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from . import config
from . import models


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

    # Open file with append mode, using file locking for safety
    fd = os.open(str(queue_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, json_line.encode())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    """Remove queue files and old transcripts older than keep_days."""
    if not config.QUEUE_DIR.exists():
        return

    cutoff = datetime.utcnow().date()
    for queue_file in config.QUEUE_DIR.glob("*.jsonl"):
        try:
            file_date = datetime.strptime(queue_file.stem, "%Y-%m-%d").date()
            age_days = (cutoff - file_date).days
            if age_days > keep_days:
                queue_file.unlink()
        except ValueError:
            # Skip files that don't match the date pattern
            continue

    # Clean up old transcript files by mtime
    if config.TRANSCRIPTS_DIR.exists():
        import time
        cutoff_ts = time.time() - (keep_days * 86400)
        for transcript_file in config.TRANSCRIPTS_DIR.glob("*.jsonl"):
            try:
                if transcript_file.stat().st_mtime < cutoff_ts:
                    transcript_file.unlink()
            except OSError:
                continue
