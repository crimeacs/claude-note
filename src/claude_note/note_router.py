"""
Note router for claude-note synthesizer.

Applies note operations from KnowledgePack to the vault.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from . import managed_blocks
from . import knowledge_pack
from . import qmd_search
from . import provenance


NOTE_TYPES = ("pattern", "gotcha", "decision", "reference", "project", "literature")


def with_type(fm: dict) -> dict:
    """The frontmatter with a `type`: the model's when valid, else the first tag
    naming a type, else `reference`. The daily push shares only typed notes, and
    before 1.6.0 no synthesized note had one, so none ever left the laptop."""
    out = dict(fm)
    given = str(out.get("type") or "").strip().lower()
    if given in NOTE_TYPES:
        out["type"] = given
        return out
    raw_tags = out.get("tags") or []
    note_tags = [str(t).strip().lower() for t in (raw_tags if isinstance(raw_tags, list) else [raw_tags])]
    out["type"] = next((t for t in note_tags if t in NOTE_TYPES), "reference")
    return out


def _format_frontmatter(fm: dict) -> str:
    """Format frontmatter dict as YAML."""
    lines = ["---"]

    for key, value in fm.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            # Quote strings with special chars
            if ":" in str(value) or '"' in str(value):
                lines.append(f'{key}: "{value}"')
            else:
                lines.append(f"{key}: {value}")

    lines.append("---")
    return "\n".join(lines)


def create_note(
    path: str,
    frontmatter: dict,
    body_markdown: str,
    vault_root: Path = None,
) -> Path:
    """
    Create a new note in the vault.

    Args:
        path: Note filename (e.g., "my-note.md")
        frontmatter: Frontmatter dict
        body_markdown: Body content
        vault_root: Override vault root

    Returns:
        Path to created note

    Raises:
        FileExistsError: If note already exists
    """
    if vault_root is None:
        vault_root = config.VAULT_ROOT

    # Ensure .md extension
    if not path.endswith(".md"):
        path = path + ".md"

    note_path = vault_root / path

    if note_path.exists():
        raise FileExistsError(f"Note already exists: {note_path}")

    # Build content
    fm_str = _format_frontmatter(frontmatter)
    content = f"{fm_str}\n\n{body_markdown}"

    # Atomic write
    temp_path = note_path.with_suffix(".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.rename(note_path)

    return note_path


def apply_note_op(op: knowledge_pack.NoteOp, vault_root: Path = None, session_id: str = None,
                  assistant: str = "") -> bool:
    """
    Apply a single note operation.

    Args:
        op: NoteOp object
        vault_root: Override vault root
        session_id: Session ID for auto-generating block IDs
        assistant: Which assistant the session came from (stamped on new notes)

    Returns:
        True if operation succeeded
    """
    if vault_root is None:
        vault_root = config.VAULT_ROOT

    # Ensure .md extension
    path = op.path
    if not path.endswith(".md"):
        path = path + ".md"

    note_path = vault_root / path

    if op.op == "create":
        if note_path.exists():
            # Note exists - use managed block for clean updates
            # Auto-generate block_id from session if not provided
            block_id = op.managed_block_id
            if not block_id:
                date = datetime.utcnow().strftime("%Y-%m-%d")
                if session_id:
                    block_id = f"synth-{session_id[:8]}-{date}"
                else:
                    block_id = f"synth-{date}"

            return managed_blocks.write_managed_block(
                note_path,
                block_id,
                op.body_markdown,
                create_if_missing=True,
            )

        frontmatter = provenance.stamp(with_type(op.frontmatter or {"tags": ["claude-note"]}), assistant)
        create_note(path, frontmatter, op.body_markdown, vault_root)
        return True

    elif op.op == "upsert_block":
        if not note_path.exists():
            return False

        block_id = op.managed_block_id or "synth"
        return managed_blocks.write_managed_block(
            note_path,
            block_id,
            op.body_markdown,
            create_if_missing=True,
        )

    elif op.op == "append":
        if not note_path.exists():
            return False

        section = op.section or "## Synthesized"
        return managed_blocks.append_to_section(
            note_path,
            section,
            op.body_markdown,
            create_section=True,
        )

    return False


def _normalize_title(title: str) -> str:
    """
    Normalize title for similarity comparison.

    Strips dates, times, common suffixes, and converts to lowercase words.
    """
    # Remove dates (YYYY-MM-DD, DD/MM/YYYY, etc.)
    title = re.sub(r'\d{4}-\d{2}-\d{2}', '', title)
    title = re.sub(r'\d{2}/\d{2}/\d{4}', '', title)

    # Remove times (HH:MM:SS, HH:MM)
    title = re.sub(r'\d{2}:\d{2}(:\d{2})?', '', title)

    # Remove common suffixes
    suffixes = [
        'session', 'update', 'continued', 'part 2', 'part 3',
        'follow-up', 'followup', 'revisited', 'debugging'
    ]
    for suffix in suffixes:
        title = re.sub(rf'\b{suffix}\b', '', title, flags=re.IGNORECASE)

    # Extract words only
    words = re.findall(r'\w+', title.lower())

    return ' '.join(words)


def _compute_similarity(title1: str, title2: str) -> float:
    """
    Compute Jaccard similarity between two normalized titles.

    Returns a value between 0 (no overlap) and 1 (identical).
    """
    words1 = set(title1.split())
    words2 = set(title2.split())

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    return len(intersection) / len(union) if union else 0.0


def _find_similar_entry(pack: knowledge_pack.KnowledgePack, inbox_path: Path, threshold: float = 0.7, lookback: int = 50) -> Optional[str]:
    """
    Check if a similar entry already exists in the inbox.

    Args:
        pack: KnowledgePack to check
        inbox_path: Path to inbox file
        threshold: Similarity threshold (0-1)
        lookback: Number of recent entries to check

    Returns:
        Matching entry title if found, None otherwise
    """
    if not inbox_path.exists():
        return None

    content = inbox_path.read_text(encoding="utf-8")

    # Extract entry titles from inbox
    entry_pattern = re.compile(r"^## (?:\d{4}-\d{2}-\d{2})(?:\s+\d{2}:\d{2}:\d{2})?\s*-\s*(.+)$", re.MULTILINE)
    matches = entry_pattern.findall(content)

    # Check only recent entries
    recent_titles = matches[:lookback] if len(matches) > lookback else matches

    # Normalize the new pack's title
    new_title_normalized = _normalize_title(pack.title)

    for existing_title in recent_titles:
        existing_normalized = _normalize_title(existing_title)
        similarity = _compute_similarity(new_title_normalized, existing_normalized)

        if similarity >= threshold:
            return existing_title

    return None


def _find_similar_content_qmd(pack: knowledge_pack.KnowledgePack, min_score: float = 0.6) -> Optional[str]:
    """
    Check if semantically similar content exists using qmd vector search.

    This catches duplicates that use different wording but cover the same topic.

    Args:
        pack: KnowledgePack to check
        min_score: Minimum similarity threshold (0-1)

    Returns:
        Matching note path if found, None otherwise
    """
    try:
        if not qmd_search.is_qmd_available():
            return None

        # Build query from pack highlights and concepts
        query_parts = []
        if pack.title:
            query_parts.append(pack.title)
        if pack.highlights:
            query_parts.extend(pack.highlights[:2])
        for concept in pack.concepts[:2]:
            query_parts.append(concept.name)

        if not query_parts:
            return None

        query = " ".join(query_parts)

        # Search for similar content
        results = qmd_search.find_similar_content(query, limit=3, min_score=min_score)

        if results:
            # Return the best match that isn't the inbox
            for result in results:
                if "inbox" not in result.path.lower():
                    return result.path

        return None

    except Exception:
        return None


def _enhance_concept_links(pack: knowledge_pack.KnowledgePack, min_score: float = 0.4) -> None:
    """
    Enhance links_suggested for concepts using semantic search.

    Post-synthesis, uses qmd to find semantically similar notes for each concept
    and adds them to links_suggested. This provides better cross-linking than
    relying on Claude to guess from note names alone.

    Args:
        pack: KnowledgePack to enhance (modified in place)
        min_score: Minimum similarity score for link suggestions
    """
    # Check if link enhancement is enabled
    if not config.QMD_LINK_ENHANCE_ENABLED:
        return

    try:
        if not qmd_search.is_qmd_available():
            return

        logger = logging.getLogger("claude-note")

        for concept in pack.concepts:
            # Build query from concept name and summary
            query_parts = [concept.name]
            if concept.summary:
                query_parts.append(concept.summary[:200])

            query = " ".join(query_parts)

            # Search for related notes
            results = qmd_search.search_vector(query, limit=5, min_score=min_score)

            # Get existing links as a set for deduplication
            existing_links = set(concept.links_suggested or [])
            added_count = 0

            for result in results:
                # Extract note name without extension
                note_name = Path(result.path).stem

                # Skip self-references and duplicates
                if note_name.lower() == concept.name.lower().replace(" ", "-"):
                    continue
                if note_name in existing_links:
                    continue
                # Skip inbox and session logs
                if "inbox" in note_name.lower() or "claude-session" in note_name.lower():
                    continue

                existing_links.add(note_name)
                added_count += 1

            # Update the concept's links_suggested
            concept.links_suggested = list(existing_links)

            if added_count > 0:
                logger.debug(f"Enhanced '{concept.name}' with {added_count} semantic links")

    except Exception as e:
        # Silent fallback - don't break routing
        logger = logging.getLogger("claude-note")
        logger.debug(f"Link enhancement failed: {e}")


def format_inbox_entry(pack: knowledge_pack.KnowledgePack) -> str:
    """
    Format a KnowledgePack as an inbox entry.

    Args:
        pack: KnowledgePack object

    Returns:
        Markdown formatted entry
    """
    lines = []

    # Header with optional time
    if pack.time:
        lines.append(f"## {pack.date} {pack.time} - {pack.title}")
    else:
        lines.append(f"## {pack.date} - {pack.title}")
    lines.append("")

    # Highlights
    if pack.highlights:
        lines.append("**Highlights:**")
        for h in pack.highlights:
            lines.append(f"- {h}")
        lines.append("")

    # Concepts
    if pack.concepts:
        lines.append("**Concepts:**")
        for c in pack.concepts:
            tags_str = " ".join(f"#{t}" for t in c.tags) if c.tags else ""
            lines.append(f"- **{c.name}**: {c.summary} {tags_str}")
        lines.append("")

    # Decisions
    if pack.decisions:
        lines.append("**Decisions:**")
        for d in pack.decisions:
            lines.append(f"- {d.decision}")
            if d.rationale:
                lines.append(f"  - *Why:* {d.rationale}")
        lines.append("")

    # Open Questions
    if pack.open_questions:
        lines.append("**Open Questions:**")
        for q in pack.open_questions:
            lines.append(f"- [ ] {q.question}")
            if q.context:
                lines.append(f"  - *Context:* {q.context}")
        lines.append("")

    # How-tos
    if pack.howtos:
        lines.append("**How-tos:**")
        for h in pack.howtos:
            lines.append(f"- **{h.title}**")
            for i, step in enumerate(h.steps, 1):
                lines.append(f"  {i}. {step}")
            if h.gotchas:
                lines.append("  - *Gotchas:*")
                for g in h.gotchas:
                    lines.append(f"    - {g}")
        lines.append("")

    # Links suggested
    all_links = set()
    for c in pack.concepts:
        all_links.update(c.links_suggested)

    if all_links:
        links_str = ", ".join(f"[[{l}]]" for l in sorted(all_links))
        lines.append(f"**Links suggested:** {links_str}")
        lines.append("")

    lines.append("---")
    lines.append("")

    return "\n".join(lines)


def append_to_inbox(pack: knowledge_pack.KnowledgePack, inbox_path: Path = None, skip_dedup: bool = False) -> Optional[Path]:
    """
    Append a KnowledgePack to the inbox file.

    Creates the inbox if it doesn't exist.
    Skips append if a similar entry already exists (deduplication).

    Args:
        pack: KnowledgePack object
        inbox_path: Override inbox path
        skip_dedup: If True, skip deduplication check

    Returns:
        Path to inbox file, or None if skipped due to duplicate
    """
    if inbox_path is None:
        inbox_path = config.INBOX_PATH

    # Check for duplicates if enabled
    if not skip_dedup and config.INBOX_DEDUP_ENABLED:
        threshold = config.INBOX_DEDUP_THRESHOLD
        lookback = config.INBOX_DEDUP_LOOKBACK

        # First: check title similarity (fast)
        similar = _find_similar_entry(pack, inbox_path, threshold, lookback)
        if similar:
            logger = logging.getLogger("claude-note")
            logger.info(f"Skipping duplicate inbox entry: '{pack.title}' similar to '{similar}'")
            return None

        # Second: check semantic similarity via qmd (if available)
        similar_content = _find_similar_content_qmd(pack, min_score=0.6)
        if similar_content:
            logger = logging.getLogger("claude-note")
            logger.info(f"Skipping semantically similar entry: '{pack.title}' similar to '{similar_content}'")
            return None

    entry = format_inbox_entry(pack)

    if not inbox_path.exists():
        # Create new inbox
        header = """---
tags:
  - log
  - claude-note
  - inbox
---

# Claude Note Inbox

Synthesized knowledge from Claude sessions. Review and promote to permanent notes.

---

"""
        inbox_path.write_text(header + entry, encoding="utf-8")
    else:
        # Prepend to existing (after header)
        current = inbox_path.read_text(encoding="utf-8")

        # Find end of header (after the "---" separator following description)
        # The pattern \n---\n\n appears twice: after frontmatter AND after header description
        # We want the SECOND occurrence (after the description, before entries)
        header_end_marker = "\n---\n\n"
        first_match = current.find(header_end_marker)

        # Look for second occurrence (the header separator, not frontmatter end)
        if first_match != -1:
            header_end = current.find(header_end_marker, first_match + 1)
        else:
            header_end = -1

        if header_end != -1:
            # Insert after header separator
            insert_pos = header_end + len(header_end_marker)
            new_content = current[:insert_pos] + entry + current[insert_pos:]
        else:
            # Fallback: prepend after frontmatter
            if current.startswith("---"):
                # Find end of frontmatter
                fm_end = current.find("\n---\n", 3)
                if fm_end != -1:
                    insert_pos = fm_end + 5  # After "\n---\n"
                    new_content = current[:insert_pos] + "\n" + entry + current[insert_pos:]
                else:
                    new_content = current + "\n" + entry
            else:
                new_content = entry + current

        inbox_path.write_text(new_content, encoding="utf-8")

    return inbox_path


def apply_note_ops(pack: knowledge_pack.KnowledgePack, mode: str = "inbox", vault_root: Path = None) -> dict:
    """
    Apply all note operations from a KnowledgePack.

    Args:
        pack: KnowledgePack object
        mode: Operation mode:
            - "inbox": Safe mode, everything goes to inbox only
            - "route": Full mode, applies note_ops to vault
        vault_root: Override vault root

    Returns:
        Dict with operation results:
            - inbox_updated: bool
            - notes_created: list[str]
            - notes_updated: list[str]
            - errors: list[str]
    """
    if vault_root is None:
        vault_root = config.VAULT_ROOT

    results = {
        "inbox_updated": False,
        "notes_created": [],
        "notes_updated": [],
        "errors": [],
    }

    # Enhance concept links using semantic search (before inbox append)
    if pack.concepts:
        try:
            _enhance_concept_links(pack)
        except Exception as e:
            # Log but don't fail
            logger = logging.getLogger("claude-note")
            logger.debug(f"Link enhancement skipped: {e}")

    # Always append to inbox (unless pack is empty or duplicate)
    if not pack.is_empty():
        try:
            inbox_result = append_to_inbox(pack)
            if inbox_result is not None:
                results["inbox_updated"] = True
            # If None, it was skipped due to deduplication (not an error)
        except Exception as e:
            results["errors"].append(f"Inbox update failed: {e}")

    # In inbox mode, we're done
    if mode == "inbox":
        return results

    # In route mode, apply note_ops
    if mode == "route":
        for op in pack.note_ops:
            try:
                # Pass session_id for auto-generating managed block IDs
                success = apply_note_op(op, vault_root, session_id=pack.session_id,
                                        assistant=pack.assistant)
                if success:
                    if op.op == "create":
                        # Check if it was actually created or fell back to update
                        note_path = vault_root / (op.path if op.path.endswith(".md") else op.path + ".md")
                        if note_path.exists():
                            # Could be either - check if we created it fresh
                            # For now, always report as created since that was the intent
                            results["notes_created"].append(op.path)
                        else:
                            results["notes_created"].append(op.path)
                    else:
                        results["notes_updated"].append(op.path)
                else:
                    results["errors"].append(f"Op failed: {op.op} {op.path}")
            except Exception as e:
                results["errors"].append(f"Op error: {op.op} {op.path}: {e}")

    return results


def get_inbox_entries(inbox_path: Path = None, limit: int = 10) -> list[dict]:
    """
    Parse recent inbox entries.

    Args:
        inbox_path: Override inbox path
        limit: Maximum entries to return

    Returns:
        List of entry dicts with date, title, highlights
    """
    if inbox_path is None:
        inbox_path = config.INBOX_PATH

    if not inbox_path.exists():
        return []

    content = inbox_path.read_text(encoding="utf-8")
    entries = []

    # Split by entry headers
    entry_pattern = re.compile(r"^## (\d{4}-\d{2}-\d{2}) - (.+)$", re.MULTILINE)

    matches = list(entry_pattern.finditer(content))

    for i, match in enumerate(matches[-limit:]):
        date = match.group(1)
        title = match.group(2)

        # Get entry content (until next entry or EOF)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        entry_content = content[start:end]

        # Extract highlights
        highlights = []
        hl_match = re.search(r"\*\*Highlights:\*\*\n((?:- .+\n)+)", entry_content)
        if hl_match:
            for line in hl_match.group(1).split("\n"):
                if line.startswith("- "):
                    highlights.append(line[2:])

        entries.append({
            "date": date,
            "title": title,
            "highlights": highlights,
        })

    return entries
