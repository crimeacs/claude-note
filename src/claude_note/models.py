"""Data models for claude-note."""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import hashlib
import json


@dataclass
class QueuedEvent:
    """An event queued for processing."""
    event_id: str           # sha256(session_id + ts + event)[:16]
    ts: str                 # ISO timestamp
    event: str              # Hook event name
    session_id: str
    cwd: str
    transcript_path: str
    data: dict              # Raw hook payload

    @classmethod
    def from_hook_input(cls, hook_data: dict) -> "QueuedEvent":
        """Create a QueuedEvent from raw hook input."""
        ts = datetime.utcnow().isoformat() + "Z"
        session_id = hook_data.get("session_id", "unknown")
        event = hook_data.get("hook_event_name", "unknown")
        cwd = hook_data.get("cwd", "")
        transcript_path = hook_data.get("transcript_path", "")

        # Generate unique event_id
        hash_input = f"{session_id}{ts}{event}"
        event_id = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

        return cls(
            event_id=event_id,
            ts=ts,
            event=event,
            session_id=session_id,
            cwd=cwd,
            transcript_path=transcript_path,
            data=hook_data,
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, json_str: str) -> "QueuedEvent":
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        return cls(**data)


@dataclass
class SessionState:
    """Tracks processing state for a session."""
    session_id: str
    first_event_ts: str                     # When session started
    last_event_ts: str                      # Last event received
    last_write_ts: Optional[str] = None     # When note was last written
    processed_event_ids: list = field(default_factory=list)
    cwd: str = ""
    transcript_path: str = ""
    events: list = field(default_factory=list)  # List of event summaries
    synthesis_pending: bool = False
    synthesis_attempts: int = 0
    last_synthesis_attempt_ts: Optional[str] = None

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "SessionState":
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        return cls(**data)

    def should_write(self, debounce_seconds: float) -> bool:
        """Check if enough time has passed since last event for debounced write."""
        if not self.last_event_ts:
            return False

        # Skip if already written after last event
        if self.last_write_ts:
            last_write = datetime.fromisoformat(self.last_write_ts.rstrip("Z"))
            last_event = datetime.fromisoformat(self.last_event_ts.rstrip("Z"))
            if last_write >= last_event:
                return False

        last_event = datetime.fromisoformat(self.last_event_ts.rstrip("Z"))
        elapsed = (datetime.utcnow() - last_event).total_seconds()
        return elapsed >= debounce_seconds


@dataclass
class EventSummary:
    """Summarized event for timeline display."""
    ts: str
    event: str
    description: str
    details: Optional[dict] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EventSummary":
        """Create from dictionary."""
        return cls(**data)
