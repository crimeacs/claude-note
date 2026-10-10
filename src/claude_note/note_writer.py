"""Markdown note generation for session notes."""

import hashlib
import re
from datetime import datetime
from pathlib import Path

from . import config
from . import models
from . import provenance


def get_note_filename(state: models.SessionState) -> str:
    """Generate note filename from session state."""
    # Parse first event timestamp for date
    first_ts = state.first_event_ts
    try:
        dt = datetime.fromisoformat(first_ts.rstrip("Z"))
        date_str = dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Use first 8 chars of session_id as short id
    short_id = state.session_id[:8]
    # Codex ids are UUIDv7: the first 8 hex digits are a timestamp shared by
    # sessions started within ~65 s of each other, so take the random tail.
    sid = state.session_id
    if len(sid) == 36 and sid[14] == "7":
        short_id = sid[-8:]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", short_id):
        short_id = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8]

    return f"claude-session-{date_str}-{short_id}.md"


def get_note_path(state: models.SessionState) -> Path:
    """Keep raw capture apart from knowledge; retain old root notes in place."""
    filename = get_note_filename(state)
    legacy_path = config.VAULT_ROOT / filename
    if legacy_path.exists():
        return legacy_path
    return config.VAULT_ROOT / "sessions" / filename


def calculate_duration(state: models.SessionState) -> str:
    """Calculate session duration as human-readable string."""
    try:
        first = datetime.fromisoformat(state.first_event_ts.rstrip("Z"))
        last = datetime.fromisoformat(state.last_event_ts.rstrip("Z"))
        delta = last - first
        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            return f"{total_seconds}s"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            seconds = total_seconds % 60
            return f"{minutes}m {seconds}s"
        else:
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{hours}h {minutes}m"
    except (ValueError, AttributeError):
        return "unknown"


def format_timestamp(ts: str) -> str:
    """Format ISO timestamp as HH:MM:SS for timeline."""
    try:
        dt = datetime.fromisoformat(ts.rstrip("Z"))
        return dt.strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return "??:??:??"


def _extract_tool_name(description: str) -> str:
    """
    Extract tool name from event description.

    Handles formats like:
    - "**Read** /path/to/file" -> "Read"
    - "**Bash** ls -la" -> "Bash"
    - "User prompt: hello" -> "UserPrompt"
    """
    # Check for **ToolName** pattern
    match = re.match(r'\*\*(\w+)\*\*', description)
    if match:
        return match.group(1)

    # Check for known event prefixes
    if description.startswith("User prompt:"):
        return "UserPrompt"
    if description.startswith("Stop:") or description.startswith("Session"):
        return "Session"

    return "Other"


def _format_group(tool_name: str, count: int, first_ts: str, last_ts: str) -> str:
    """Format a group of consecutive same-type tool operations."""
    time_str = format_timestamp(first_ts)
    if count == 1:
        return f"- `{time_str}` **{tool_name}**"

    # Show time range for groups
    end_time = format_timestamp(last_ts)
    return f"- `{time_str}-{end_time}` **{tool_name}** x{count}"


def compress_timeline(events: list, max_entries: int = None) -> list:
    """
    Compress timeline by grouping consecutive same-type tool operations.

    Args:
        events: List of event dicts with 'ts', 'event', 'description'
        max_entries: Maximum entries (defaults to config.TIMELINE_MAX_ENTRIES)

    Returns:
        List of compressed event groups as dicts:
        {'tool': str, 'count': int, 'first_ts': str, 'last_ts': str, 'sample_desc': str}
    """
    if max_entries is None:
        max_entries = config.TIMELINE_MAX_ENTRIES

    if not events:
        return []

    # Build groups of consecutive same-tool operations
    groups = []
    current_group = None

    for event_dict in events:
        event = models.EventSummary.from_dict(event_dict)
        tool_name = _extract_tool_name(event.description)

        if current_group is None:
            current_group = {
                'tool': tool_name,
                'count': 1,
                'first_ts': event.ts,
                'last_ts': event.ts,
                'sample_desc': event.description,
            }
        elif current_group['tool'] == tool_name:
            # Same tool, extend group
            current_group['count'] += 1
            current_group['last_ts'] = event.ts
        else:
            # Different tool, close current group and start new one
            groups.append(current_group)
            current_group = {
                'tool': tool_name,
                'count': 1,
                'first_ts': event.ts,
                'last_ts': event.ts,
                'sample_desc': event.description,
            }

    # Don't forget the last group
    if current_group:
        groups.append(current_group)

    # If we're under the limit, return as-is
    if len(groups) <= max_entries:
        return groups

    # Need to compress further - keep first 10, last 10, sample middle
    if len(groups) > 20:
        first_part = groups[:10]
        last_part = groups[-10:]
        middle_count = len(groups) - 20

        # Create a summary group for the middle
        middle_groups = groups[10:-10]
        tool_counts = {}
        for g in middle_groups:
            tool_counts[g['tool']] = tool_counts.get(g['tool'], 0) + g['count']

        summary = ", ".join(f"{t}x{c}" for t, c in sorted(tool_counts.items(), key=lambda x: -x[1]))
        middle_summary = {
            'tool': 'Summary',
            'count': sum(g['count'] for g in middle_groups),
            'first_ts': middle_groups[0]['first_ts'],
            'last_ts': middle_groups[-1]['last_ts'],
            'sample_desc': f"... {middle_count} operations ({summary}) ...",
        }

        return first_part + [middle_summary] + last_part

    return groups


def format_timeline(events: list, compress: bool = True) -> str:
    """
    Format events as timeline markdown.

    Args:
        events: List of event dicts
        compress: If True, compress consecutive same-type operations
    """
    if not events:
        return "(No events recorded)"

    # Use compression if enabled and we have many events
    max_entries = config.TIMELINE_MAX_ENTRIES
    if compress and len(events) > max_entries:
        groups = compress_timeline(events, max_entries)
        lines = []

        for group in groups:
            if group['tool'] == 'Summary':
                # Special summary line
                time_str = format_timestamp(group['first_ts'])
                lines.append(f"- `{time_str}` {group['sample_desc']}")
            elif group['count'] == 1:
                # Single event, show full description
                time_str = format_timestamp(group['first_ts'])
                lines.append(f"- `{time_str}` {group['sample_desc']}")
            else:
                # Grouped events
                lines.append(_format_group(
                    group['tool'],
                    group['count'],
                    group['first_ts'],
                    group['last_ts']
                ))

        return "\n".join(lines)

    # Standard format for smaller timelines
    lines = []
    for event_dict in events:
        event = models.EventSummary.from_dict(event_dict)
        time_str = format_timestamp(event.ts)
        lines.append(f"- `{time_str}` {event.description}")

    return "\n".join(lines)


def generate_note_content(state: models.SessionState) -> str:
    """Generate full markdown content for session note."""
    # Get date for frontmatter
    try:
        dt = datetime.fromisoformat(state.first_event_ts.rstrip("Z"))
        date_str = dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    duration = calculate_duration(state)
    timeline = format_timeline(state.events)
    provenance_lines = "".join(
        f"{key}: {value}\n" for key, value in provenance.stamp(
            {}, provenance.assistant_for(state.transcript_path)).items())

    # Build the note
    content = f"""---
type: session
tags:
  - log
  - claude-note
aliases: []
created: {date_str}
session_id: {state.session_id}
{provenance_lines}---

# Claude Session {date_str}

**Working directory:** `{state.cwd}`
**Duration:** {duration}

## Summary

(Updated on Stop/SessionEnd with session highlights)

## Timeline

{timeline}

## Decisions

(Empty in MVP - reserved for future use)

## Open Questions

(Questions discovered during session)

"""

    return content


def write_session_note(state: models.SessionState) -> Path:
    """
    Write session note to vault.

    Uses atomic write (temp file + rename) to prevent corruption.
    Returns the path to the written note.
    """
    note_path = get_note_path(state)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    content = generate_note_content(state)

    # Atomic write
    temp_path = note_path.with_suffix(".tmp")
    temp_path.write_text(content)
    temp_path.rename(note_path)

    return note_path


def update_session_note(state: models.SessionState) -> Path:
    """
    Update existing session note or create new one.

    If note exists, preserves user-edited sections while updating timeline.
    """
    note_path = get_note_path(state)

    if not note_path.exists():
        return write_session_note(state)

    # Read existing content
    existing = note_path.read_text()

    # Parse and update only the Timeline section
    # This preserves any manual edits to Summary, Decisions, etc.
    lines = existing.split("\n")
    new_lines = []
    in_timeline = False
    timeline_replaced = False

    i = 0
    while i < len(lines):
        line = lines[i]

        if line.strip() == "## Timeline":
            in_timeline = True
            new_lines.append(line)
            new_lines.append("")
            new_lines.append(format_timeline(state.events))
            new_lines.append("")
            i += 1
            # Skip old timeline content until next section
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            timeline_replaced = True
            continue

        if in_timeline and line.startswith("## "):
            in_timeline = False

        new_lines.append(line)
        i += 1

    # Also update duration in header
    new_content = "\n".join(new_lines)
    duration = calculate_duration(state)

    # Update duration line if present
    new_content = re.sub(
        r"\*\*Duration:\*\* .+",
        f"**Duration:** {duration}",
        new_content
    )

    # Atomic write
    temp_path = note_path.with_suffix(".tmp")
    temp_path.write_text(new_content)
    temp_path.rename(note_path)

    return note_path
