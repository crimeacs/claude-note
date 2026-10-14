"""Session state tracking with locks and debouncing."""

import fcntl
import logging
import os
import time
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import config
from . import models
from .state_io import atomic_json, read_json, open_lock


def _session_id(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        raise ValueError("Invalid session identity")
    return session_id


def get_state_file(session_id: str) -> Path:
    """Get the state file path for a session."""
    return config.STATE_DIR / f"{_session_id(session_id)}.json"


def get_lock_file(session_id: str) -> Path:
    """Get the lock file path for a session."""
    return config.STATE_DIR / f"{_session_id(session_id)}.lock"


@contextmanager
def session_lock(session_id: str, timeout: float = None) -> Iterator[bool]:
    """
    Context manager for session locking.

    Yields True if lock acquired, False if timeout.
    """
    if timeout is None:
        timeout = config.LOCK_TIMEOUT

    lock_file = get_lock_file(session_id)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    fd = None
    acquired = False
    start_time = time.time()

    try:
        fd = open_lock(lock_file)

        # Try to acquire lock with timeout
        while time.time() - start_time < timeout:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.1)

        yield acquired
    finally:
        if fd is not None:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def load_session_state(session_id: str) -> Optional[models.SessionState]:
    """Load session state from file."""
    state_file = get_state_file(session_id)
    if not state_file.exists():
        return None

    try:
        return models.SessionState(**read_json(state_file))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Existing session state is unreadable or incompatible; preserve it for recovery") from exc


def save_session_state(state: models.SessionState) -> None:
    """Save session state to file."""
    state_file = get_state_file(state.session_id)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    import json
    atomic_json(state_file, json.loads(state.to_json()))


def is_recursive_event(event: models.QueuedEvent) -> bool:
    """Check if event is from claude-note itself (recursion prevention)."""
    # Check cwd
    if any(marker in event.cwd for marker in config.RECURSION_MARKERS):
        return True

    # Check tool inputs in data
    data = event.data
    tool_input = data.get("tool_input", {})

    # Check file paths
    file_path = tool_input.get("file_path", "")
    if any(marker in file_path for marker in config.RECURSION_MARKERS):
        return True

    # Check bash commands
    command = tool_input.get("command", "")
    if any(marker in command for marker in config.RECURSION_MARKERS):
        return True

    # Check grep/glob patterns
    pattern = tool_input.get("pattern", "")
    if any(marker in pattern for marker in config.RECURSION_MARKERS):
        return True

    # Check path parameter
    path = tool_input.get("path", "")
    if any(marker in path for marker in config.RECURSION_MARKERS):
        return True

    # Check user prompt (for synthesis sessions)
    prompt = data.get("prompt", "")
    if any(marker in prompt for marker in config.RECURSION_MARKERS):
        return True

    return False


def extract_event_summary(event: models.QueuedEvent) -> models.EventSummary:
    """Extract a human-readable summary from an event."""
    ts = event.ts
    event_type = event.event
    data = event.data

    if event_type == "SessionStart":
        description = "Session started"
    elif event_type == "SessionEnd":
        description = "Session ended"
    elif event_type == "Stop":
        description = "Session stopped"
    elif event_type == "PreCompact":
        description = "Context compaction"
    elif event_type == "UserPromptSubmit":
        prompt = data.get("prompt", "")
        if len(prompt) > 80:
            prompt = prompt[:77] + "..."
        description = f'User prompt: "{prompt}"'
    elif event_type in ("PostToolUse", "PostToolUseFailure"):
        tool_name = data.get("tool_name", "unknown")
        tool_input = data.get("tool_input", {})

        # Build tool-specific description
        if tool_name == "Read":
            file_path = tool_input.get("file_path", "")
            description = f"**Read** `{Path(file_path).name}`"
        elif tool_name == "Write":
            file_path = tool_input.get("file_path", "")
            description = f"**Write** `{Path(file_path).name}`"
        elif tool_name == "Edit":
            file_path = tool_input.get("file_path", "")
            description = f"**Edit** `{Path(file_path).name}`"
        elif tool_name == "Bash":
            command = tool_input.get("command", "")
            if len(command) > 60:
                command = command[:57] + "..."
            description = f"**Bash** `{command}`"
        elif tool_name == "Grep":
            pattern = tool_input.get("pattern", "")
            description = f"**Grep** `{pattern}`"
        elif tool_name == "Glob":
            pattern = tool_input.get("pattern", "")
            description = f"**Glob** `{pattern}`"
        elif tool_name == "Task":
            desc = tool_input.get("description", "")
            description = f"**Task** {desc}"
        else:
            description = f"**{tool_name}**"

        if event_type == "PostToolUseFailure":
            description += " (failed)"
    else:
        description = event_type

    return models.EventSummary(ts=ts, event=event_type, description=description)


def _event_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def update_session_from_events(session_id: str, events: list) -> models.SessionState:
    """
    Update or create session state from events.

    Filters out recursive events and deduplicates by event_id.
    """
    from . import import_publication

    # Verify every reference before any capture acknowledgement, including
    # already-seen IDs and revisions that will not become the active view.
    verified = {}
    for event in events:
        event.validate()
        if event.session_id != session_id:
            raise ValueError("Event session differs from capture target")
        publication = import_publication.validate_event(event)
        if publication:
            verified[event.event_id] = publication
    state = load_session_state(session_id)
    if state is None:
        if not events:
            raise ValueError("Cannot create session state with no events")
        first_event = events[0]
        state = models.SessionState(session_id=session_id, first_event_ts=first_event.ts,
                                   last_event_ts="", cwd=first_event.cwd,
                                   transcript_path=first_event.transcript_path)
    if not all(isinstance(value, dict) for value in
               (state.import_binding, state.import_ordinals, state.import_publications)):
        raise ValueError("Invalid persisted import capture state")
    processed_ids = set(state.processed_event_ids)
    accepted_completion = False
    # Delivery filenames and hash order do not define evidence freshness.
    # Ordinals order imports from one logical stream; local creation timestamps
    # delimit newer hooks. Clock rollback across hooks remains a limitation.
    for event in sorted(events, key=lambda event: (_event_time(event.ts), event.event_id)):
        if event.event_id in processed_ids:
            continue
        publication = verified.get(event.event_id)
        if publication:
            identity, stream = publication["publication_id"], publication["stream"]
            ordinal = publication["ordinal"]
            old_ordinal = state.import_ordinals.get(stream, 0)
            if type(old_ordinal) is not int or old_ordinal < 0:
                raise ValueError("Invalid persisted import stream ordinal")
            if len(state.import_publications) >= 10000 and identity not in state.import_publications:
                raise ValueError("Import history exceeds bounded session state; preserve for recovery")
            state.import_publications[identity] = import_publication.binding(publication)
            stale = ordinal < old_ordinal or (state.last_native_event_ts and
                                             _event_time(event.ts) < _event_time(state.last_native_event_ts))
            # Advance the stream high-water even when a newer hook superseded it.
            state.import_ordinals[stream] = max(old_ordinal, ordinal)
        else:
            stale = bool(state.last_event_ts and _event_time(event.ts) < _event_time(state.last_event_ts))
        state.processed_event_ids.append(event.event_id)
        processed_ids.add(event.event_id)
        if stale or is_recursive_event(event):
            state.filtered_event_ids[event.event_id] = "stale-source-binding" if stale else "recursion-policy"
            continue
        if publication:
            if state.import_binding.get("publication_id") != publication["publication_id"]:
                # A new accepted immutable revision is work even under clock
                # rollback; last_event_ts itself must never move backwards.
                state.last_write_ts = None
            state.import_binding = import_publication.binding(publication)
        else:
            # A genuinely newer hook resumes the mutable native transcript.
            state.import_binding = {}
            state.last_native_event_ts = event.ts
        if not state.last_event_ts or _event_time(event.ts) > _event_time(state.last_event_ts):
            state.last_event_ts = event.ts
        if event.cwd:
            state.cwd = event.cwd
        if event.transcript_path:
            state.transcript_path = event.transcript_path
        summary = extract_event_summary(event).to_dict()
        if publication:
            summary["details"] = {"publication_id": publication["publication_id"],
                                  "captured_revision": publication["captured_revision"],
                                  "logical_revision": publication["logical_revision"]}
        state.events.append(summary)
        accepted_completion |= event.event in {"Stop", "SessionEnd"}

    # Persist the completed turn's outstanding action with its captured IDs.
    # A crash after capture cannot consume the only request for synthesis.
    if accepted_completion and any(event.get("event") == "UserPromptSubmit" for event in state.events):
        schedule_synthesis(state)

    return state


def should_flush_immediately(events: list) -> bool:
    """Check if session should be flushed immediately (Stop/SessionEnd)."""
    immediate_events = {"Stop", "SessionEnd"}
    return any(e.event in immediate_events for e in events)


def get_sessions_ready_for_write(debounce_seconds: float = None) -> list[str]:
    """Get session IDs that are ready for note writing."""
    if debounce_seconds is None:
        debounce_seconds = config.DEBOUNCE_SECONDS

    ready = []

    if not config.STATE_DIR.exists():
        return ready

    for state_file in config.STATE_DIR.glob("*.json"):
        session_id = state_file.stem
        try:
            state = load_session_state(session_id)
            if state and state.should_write(debounce_seconds):
                ready.append(session_id)
        except (OSError, ValueError, TypeError):
            logging.getLogger(__name__).warning("Incompatible session state retained; readiness scan continues")

    return ready


def mark_session_written(session_id: str) -> None:
    """Mark session as having been written."""
    state = load_session_state(session_id)
    if state:
        state.last_write_ts = datetime.utcnow().isoformat() + "Z"
        save_session_state(state)


def is_session_written(state: models.SessionState) -> bool:
    """Check if session has already been written after its last event."""
    if not state.last_write_ts or not state.last_event_ts:
        return False
    last_write = datetime.fromisoformat(state.last_write_ts.rstrip("Z"))
    last_event = datetime.fromisoformat(state.last_event_ts.rstrip("Z"))
    return last_write >= last_event


def schedule_synthesis(state: models.SessionState) -> None:
    """A new completed turn supersedes the previous synthesis retry schedule."""
    state.synthesis_pending = config.SYNTH_MODE != "log" and bool(state.transcript_path)
    state.synthesis_attempts = 0
    state.last_synthesis_attempt_ts = None
    state.synthesis_parked = False
    state.synthesis_park_reason = ""


def synthesis_retry_due(state: models.SessionState) -> bool:
    if not state.synthesis_pending or state.synthesis_parked:
        return False
    if not state.last_synthesis_attempt_ts:
        return True
    try:
        last = datetime.fromisoformat(state.last_synthesis_attempt_ts.rstrip("Z"))
        delay = min(3600, 60 * 2 ** min(max(state.synthesis_attempts - 1, 0), 6))
        return (datetime.utcnow() - last).total_seconds() >= delay
    except (ValueError, TypeError):
        return True


def run_pending_synthesis(state: models.SessionState, synthesize: Callable,
                          force: bool = False) -> Optional[bool]:
    """Retry synthesis separately from note writing, persisting crash recovery."""
    if not state.synthesis_pending or (not force and not synthesis_retry_due(state)):
        return None
    if config.SYNTH_MODE == "log":
        state.synthesis_pending = False
        save_session_state(state)
        return None
    state.synthesis_attempts += 1
    state.last_synthesis_attempt_ts = datetime.utcnow().isoformat() + "Z"
    save_session_state(state)
    success = bool(synthesize(state))
    state.synthesis_pending = not success
    if not success and state.synthesis_attempts >= 8:
        state.synthesis_parked = True
        state.synthesis_park_reason = "retry-limit: inspect extraction/operation errors, then explicitly resynthesize"
    if success and state.extraction_checkpoint:
        # Do not read the live source here: a new turn may have arrived while
        # the model was running and is not part of this extraction generation.
        state.transcript_checkpoint = dict(state.extraction_checkpoint)
    save_session_state(state)
    return success


def get_pending_synthesis_sessions() -> list[str]:
    """Retries survive queue retention and a worker restart."""
    if not config.STATE_DIR.is_dir():
        return []
    pending = []
    for path in config.STATE_DIR.glob("*.json"):
        try:
            state = load_session_state(path.stem)
            if state and state.synthesis_pending and not state.synthesis_parked:
                pending.append(state.session_id)
        except (OSError, ValueError, TypeError):
            logging.getLogger(__name__).warning("Incompatible session state retained; synthesis scan continues")
    return pending
