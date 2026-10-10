"""
Daily cleanup for claude-note.

Provides tools to clean up bloated notes, deduplicate inbox entries,
compress session timelines, and remove orphan state files.
"""

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from . import models
from . import note_writer
from . import managed_blocks


def clean_state_dir(max_age_days: int = 7, dry_run: bool = True) -> dict:
    """
    Clean up orphan lock files and old completed session states.

    Args:
        max_age_days: Remove state files older than this many days
        dry_run: If True, only report what would be done

    Returns:
        Dict with 'locks_removed', 'states_removed', 'bytes_freed'
    """
    state_dir = config.STATE_DIR
    note_locks_dir = state_dir / "note_locks"

    results = {
        "locks_removed": 0,
        "states_removed": 0,
        "bytes_freed": 0,
    }

    if not state_dir.exists():
        return results

    cutoff = time.time() - (max_age_days * 86400)

    # Clean up orphan lock files
    if note_locks_dir.exists():
        for lock_file in note_locks_dir.glob("*.lock"):
            try:
                stat = lock_file.stat()
                if stat.st_mtime < cutoff:
                    results["bytes_freed"] += stat.st_size
                    results["locks_removed"] += 1
                    if not dry_run:
                        lock_file.unlink()
            except (OSError, IOError):
                pass

    # Clean up old session state files
    for state_file in state_dir.glob("*.json"):
        try:
            stat = state_file.stat()
            if stat.st_mtime < cutoff:
                # Retry state is the durable work item once queue files age out.
                # Leave unreadable state in place for recovery, too.
                try:
                    state_data = json.loads(state_file.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if not isinstance(state_data, dict) or state_data.get("synthesis_pending"):
                    continue
                results["bytes_freed"] += stat.st_size
                results["states_removed"] += 1
                if not dry_run:
                    state_file.unlink()
        except (OSError, IOError):
            pass

    # Clean up old lock files in root state dir
    for lock_file in state_dir.glob("*.lock"):
        try:
            stat = lock_file.stat()
            # Lock files should be very short-lived; remove if older than 1 hour
            if stat.st_mtime < time.time() - 3600:
                results["bytes_freed"] += stat.st_size
                results["locks_removed"] += 1
                if not dry_run:
                    lock_file.unlink()
        except (OSError, IOError):
            pass

    return results


def compress_session_timeline(note_path: Path, dry_run: bool = True) -> Optional[dict]:
    """
    Compress verbose timeline in a session note.

    Replaces the Timeline section with a compressed version.

    Args:
        note_path: Path to session note
        dry_run: If True, only report what would be done

    Returns:
        Dict with 'original_lines', 'compressed_lines', 'saved_bytes'
        or None if no compression needed
    """
    if not note_path.exists():
        return None

    content = note_path.read_text(encoding="utf-8")
    lines = content.split("\n")

    # Find Timeline section
    timeline_start = None
    timeline_end = None

    for i, line in enumerate(lines):
        if line.strip() == "## Timeline":
            timeline_start = i
        elif timeline_start is not None and line.startswith("## "):
            timeline_end = i
            break

    if timeline_start is None:
        return None

    if timeline_end is None:
        timeline_end = len(lines)

    # Extract timeline content
    timeline_lines = lines[timeline_start + 1:timeline_end]
    timeline_content = "\n".join(timeline_lines).strip()

    # Count original entries
    original_entries = [l for l in timeline_lines if l.strip().startswith("- `")]

    if len(original_entries) <= 100:
        # No compression needed
        return None

    # Parse events from timeline
    events = []
    entry_pattern = re.compile(r"^- `(\d{2}:\d{2}:\d{2})` (.+)$")

    for line in timeline_lines:
        match = entry_pattern.match(line.strip())
        if match:
            time_str, desc = match.groups()
            events.append({
                "ts": f"2000-01-01T{time_str}Z",  # Dummy date for parsing
                "event": "parsed",
                "description": desc,
            })

    if not events:
        return None

    # Use note_writer compression
    compressed = note_writer.format_timeline(events, compress=True)

    # Count compressed entries
    compressed_entries = [l for l in compressed.split("\n") if l.strip().startswith("- `")]

    original_bytes = len(timeline_content.encode("utf-8"))
    compressed_bytes = len(compressed.encode("utf-8"))

    results = {
        "original_lines": len(original_entries),
        "compressed_lines": len(compressed_entries),
        "saved_bytes": original_bytes - compressed_bytes,
    }

    if not dry_run and results["saved_bytes"] > 0:
        # Replace timeline section
        new_lines = (
            lines[:timeline_start + 1]
            + ["", compressed, ""]
            + lines[timeline_end:]
        )
        new_content = "\n".join(new_lines)

        # Atomic write
        temp_path = note_path.with_suffix(".tmp")
        temp_path.write_text(new_content, encoding="utf-8")
        temp_path.rename(note_path)

    return results


def dedupe_inbox(inbox_path: Path = None, similarity_threshold: float = 0.7, dry_run: bool = True) -> dict:
    """
    Deduplicate exact inbox extractions, preserving new evidence and corrections.

    New entries carry extraction fingerprints. Legacy entries are comparable
    only by identical body content, ignoring their date/time/title header.

    Args:
        inbox_path: Path to inbox file (defaults to config)
        similarity_threshold: Legacy argument retained for call compatibility; ignored
        dry_run: If True, only report proposed merges

    Returns:
        Dict with 'total_entries', 'duplicate_groups', 'entries_removed'
    """
    if inbox_path is None:
        inbox_path = config.INBOX_PATH

    results = {
        "total_entries": 0,
        "duplicate_groups": [],
        "entries_removed": 0,
    }

    if not inbox_path.exists():
        return results

    content = inbox_path.read_text(encoding="utf-8")

    # Parse entries
    entry_pattern = re.compile(
        r"^## (\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}:\d{2}))?\s*-\s*(.+)$",
        re.MULTILINE
    )

    matches = list(entry_pattern.finditer(content))
    results["total_entries"] = len(matches)

    if len(matches) < 2:
        return results

    # Build exact groups. An unchanged marker alone is insufficient: a human
    # may have annotated the entry after capture, so compare its body as well.
    entries = []
    for index, match in enumerate(matches):
        date, time_str, title = match.groups()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end():end].strip()
        marker = re.search(r"<!-- claude-note:extraction:([a-f0-9]{64}) -->", body)
        without_marker = re.sub(r"<!-- claude-note:extraction:[a-f0-9]{64} -->", "", body).strip()
        fingerprint = marker.group(1) if marker else "legacy"
        body_hash = hashlib.sha256(without_marker.encode("utf-8")).hexdigest()
        entries.append({
            "date": date,
            "time": time_str or "",
            "title": title,
            "identity": (fingerprint, body_hash),
            "start": match.start(),
            "end": end,
        })

    # Find duplicate groups
    used = set()
    groups = []

    for i, entry in enumerate(entries):
        if i in used:
            continue

        group = [entry]
        used.add(i)

        for j, other in enumerate(entries):
            if j in used or j <= i:
                continue

            if entry["identity"] == other["identity"]:
                group.append(other)
                used.add(j)

        if len(group) > 1:
            groups.append(group)

    results["duplicate_groups"] = [
        {
            "count": len(g),
            "titles": [e["title"] for e in g],
            "dates": [e["date"] for e in g],
        }
        for g in groups
    ]

    # Count entries that would be removed (keep 1 per group)
    results["entries_removed"] = sum(len(g) - 1 for g in groups)

    # If not dry run, actually remove duplicates (keep the most recent)
    if not dry_run and groups:
        # Sort entries in each group by date descending, keep first
        entries_to_remove = []
        for group in groups:
            sorted_group = sorted(group, key=lambda e: e["date"], reverse=True)
            # Keep the first (most recent), remove the rest
            for entry in sorted_group[1:]:
                entries_to_remove.append(entry)

        if entries_to_remove:
            # Remove entries from content (process in reverse order to preserve positions)
            entries_to_remove.sort(key=lambda e: e["start"], reverse=True)

            new_content = content
            for entry in entries_to_remove:
                # Remove from start of ## to start of next ## (or EOF)
                new_content = new_content[:entry["start"]] + new_content[entry["end"]:]

            # Atomic write
            temp_path = inbox_path.with_suffix(".tmp")
            temp_path.write_text(new_content, encoding="utf-8")
            temp_path.rename(inbox_path)

    return results


def consolidate_managed_blocks(note_path: Path, dry_run: bool = True) -> dict:
    """
    Consolidate redundant managed blocks in a topic note.

    Identifies blocks with identical content; similar wording can be a correction.

    Args:
        note_path: Path to the note file
        dry_run: If True, only report proposed consolidations

    Returns:
        Dict with 'total_blocks', 'redundant_groups', 'blocks_removed'
    """
    results = {
        "total_blocks": 0,
        "redundant_groups": [],
        "blocks_removed": 0,
    }

    if not note_path.exists():
        return results

    block_ids = managed_blocks.list_managed_blocks(note_path)
    results["total_blocks"] = len(block_ids)

    if len(block_ids) < 2:
        return results

    # Read content of each block
    blocks = []
    for block_id in block_ids:
        content = managed_blocks.read_managed_block(note_path, block_id)
        if content:
            blocks.append({
                "id": block_id,
                "content": content,
            })

    # Find exact duplicate blocks; word overlap is not a deletion criterion.
    used = set()
    groups = []

    for i, block in enumerate(blocks):
        if i in used:
            continue

        group = [block]
        used.add(i)

        for j, other in enumerate(blocks):
            if j in used or j <= i:
                continue

            if block["content"].strip() == other["content"].strip():
                group.append(other)
                used.add(j)

        if len(group) > 1:
            groups.append(group)

    results["redundant_groups"] = [
        {
            "count": len(g),
            "block_ids": [b["id"] for b in g],
            "sample": g[0]["content"][:100] + "..." if len(g[0]["content"]) > 100 else g[0]["content"],
        }
        for g in groups
    ]

    results["blocks_removed"] = sum(len(g) - 1 for g in groups)

    # If not dry run, remove redundant blocks (keep first)
    if not dry_run and groups:
        for group in groups:
            # Keep the first block, delete the rest
            for block in group[1:]:
                managed_blocks.delete_managed_block(note_path, block["id"])

    return results


def find_session_notes(date: str = None) -> list[Path]:
    """
    Find session notes, optionally filtered by date.

    Args:
        date: ISO date string (YYYY-MM-DD) to filter by, or None for all

    Returns:
        List of session note paths
    """
    vault_root = config.VAULT_ROOT

    pattern = f"claude-session-{date}-*.md" if date else "claude-session-*.md"
    return sorted(path for path in [*vault_root.glob(pattern), *(vault_root / "sessions").glob(pattern)]
                  if path.is_file() and not path.is_symlink())


def find_topic_notes() -> list[Path]:
    """
    Find topic notes (non-session, non-inbox notes).

    Returns:
        List of topic note paths
    """
    vault_root = config.VAULT_ROOT

    notes = []
    for note_path in vault_root.glob("*.md"):
        name = note_path.name
        # Skip session notes and inbox
        if name.startswith("claude-session-"):
            continue
        if name == "claude-note-inbox.md":
            continue
        # Skip templates
        if note_path.parent.name == "templates":
            continue

        notes.append(note_path)

    return sorted(notes)


def run_daily_clean(
    date: str = None,
    dry_run: bool = True,
    clean_state: bool = True,
    clean_sessions: bool = True,
    clean_inbox: bool = True,
    clean_topics: bool = True,
) -> dict:
    """
    Run daily cleanup operations.

    Args:
        date: ISO date string (YYYY-MM-DD) to focus on, or None for today
        dry_run: If True, only report what would be done
        clean_state: Clean orphan lock files and old state
        clean_sessions: Compress session timelines
        clean_inbox: Deduplicate inbox entries
        clean_topics: Consolidate redundant blocks in topic notes

    Returns:
        Dict with results from each cleanup operation
    """
    if date is None:
        date = datetime.utcnow().strftime("%Y-%m-%d")

    results = {
        "date": date,
        "dry_run": dry_run,
        "state": None,
        "sessions": [],
        "inbox": None,
        "topics": [],
    }

    # Clean state directory
    if clean_state:
        results["state"] = clean_state_dir(max_age_days=7, dry_run=dry_run)

    # Compress session timelines
    if clean_sessions:
        for note_path in find_session_notes(date):
            session_result = compress_session_timeline(note_path, dry_run=dry_run)
            if session_result:
                session_result["note"] = note_path.name
                results["sessions"].append(session_result)

    # Deduplicate inbox
    if clean_inbox:
        results["inbox"] = dedupe_inbox(dry_run=dry_run)

    # Consolidate topic notes
    if clean_topics:
        for note_path in find_topic_notes():
            topic_result = consolidate_managed_blocks(note_path, dry_run=dry_run)
            if topic_result and topic_result["redundant_groups"]:
                topic_result["note"] = note_path.name
                results["topics"].append(topic_result)

    return results


def format_clean_results(results: dict) -> str:
    """Format cleanup results as human-readable text."""
    lines = [
        f"=== Claude Note Cleanup {'(dry-run)' if results['dry_run'] else ''} ===",
        f"Date: {results['date']}",
        "",
    ]

    # State cleanup
    if results["state"]:
        s = results["state"]
        lines.append("State directory:")
        lines.append(f"  Locks removed: {s['locks_removed']}")
        lines.append(f"  States removed: {s['states_removed']}")
        lines.append(f"  Bytes freed: {s['bytes_freed']:,}")
        lines.append("")

    # Session compression
    if results["sessions"]:
        lines.append("Session timelines compressed:")
        for s in results["sessions"]:
            lines.append(f"  {s['note']}: {s['original_lines']} -> {s['compressed_lines']} lines ({s['saved_bytes']:,} bytes saved)")
        lines.append("")
    else:
        lines.append("Session timelines: (none needed compression)")
        lines.append("")

    # Inbox dedup
    if results["inbox"]:
        i = results["inbox"]
        lines.append("Inbox deduplication:")
        lines.append(f"  Total entries: {i['total_entries']}")
        lines.append(f"  Duplicate groups found: {len(i['duplicate_groups'])}")
        lines.append(f"  Entries to remove: {i['entries_removed']}")
        if i["duplicate_groups"]:
            lines.append("  Groups:")
            for g in i["duplicate_groups"][:5]:  # Show first 5
                lines.append(f"    - {g['count']} entries: {g['titles'][0][:50]}...")
        lines.append("")

    # Topic consolidation
    if results["topics"]:
        lines.append("Topic notes with redundant blocks:")
        for t in results["topics"]:
            lines.append(f"  {t['note']}: {t['total_blocks']} blocks, {t['blocks_removed']} redundant")
            for g in t["redundant_groups"][:2]:  # Show first 2 groups
                lines.append(f"    - {g['count']} similar: {g['block_ids']}")
        lines.append("")
    else:
        lines.append("Topic notes: (none had redundant blocks)")
        lines.append("")

    return "\n".join(lines)
