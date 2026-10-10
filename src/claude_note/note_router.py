"""
Note router for claude-note synthesizer.

Applies note operations from KnowledgePack to the vault.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from . import config
from . import managed_blocks
from . import knowledge_pack
from . import qmd_search
from . import provenance


NOTE_TYPES = knowledge_pack.NOTE_TYPES
logger = logging.getLogger(__name__)


def with_type(fm: dict) -> dict:
    """Keep an explicit semantic type, with a visible legacy fallback.

    Types describe the role of a note; they do not grant publishing eligibility.
    Tag inference is only a compatibility aid for older extraction output.
    """
    out = dict(fm)
    given = str(out.get("type") or "").strip().lower()
    if given in NOTE_TYPES:
        out["type"] = given
        return {"type": out.pop("type"), **out}
    raw_tags = out.get("tags") or []
    note_tags = [str(t).strip().lower() for t in (raw_tags if isinstance(raw_tags, list) else [raw_tags])]
    out["type"] = next((t for t in note_tags if t in NOTE_TYPES), "reference")
    logger.warning("Missing or invalid note type %r; using %r. Set type explicitly to preserve the note's meaning.",
                   fm.get("type"), out["type"])
    return {"type": out.pop("type"), **out}


def resolve_note_path(path: str, vault_root: Path) -> Path:
    """Resolve an untrusted Markdown route within the vault's knowledge area.

    A model route must never reach outside the vault or mutate its settings,
    templates, capture logs, or agent instructions.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Note path must be a non-empty relative Markdown path")
    if path != path.strip() or "\\" in path or ":" in path or any(ord(c) < 32 for c in path):
        raise ValueError(f"Invalid note path: {path!r}")
    candidate = Path(path)
    if candidate.is_absolute() or any(p in (".", "..") or p.startswith(".") for p in path.split("/")):
        raise ValueError(f"Note path must stay inside the vault: {path!r}")
    if not candidate.suffix:
        candidate = candidate.with_suffix(".md")
    elif candidate.suffix.lower() != ".md":
        raise ValueError(f"Note path must use the .md extension: {path!r}")
    legacy_session = re.fullmatch(r"claude-session-\d{4}-\d{2}-\d{2}-.+\.md", candidate.name, re.IGNORECASE)
    if (candidate.parts[0].lower() in ("templates", "sessions")
            or candidate.name.lower() in ("claude.md", "agents.md") or legacy_session):
        raise ValueError(f"Note path targets a protected vault file: {path!r}")
    root = Path(vault_root).resolve()
    target = root / candidate
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"Note path escapes the vault: {path!r}")
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError(f"Note path traverses a symlink: {path!r}")
    return target


def _format_frontmatter(fm: dict) -> str:
    """Format frontmatter dict as YAML."""
    lines = ["---"]

    for key, value in fm.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise ValueError(f"Invalid frontmatter key: {key!r}")
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {json.dumps(str(item), ensure_ascii=False)}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            # JSON strings are YAML-compatible and safely quote newlines,
            # colons, quotes and values YAML would otherwise coerce.
            lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")

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

    note_path = resolve_note_path(path, vault_root)

    if note_path.exists():
        raise FileExistsError(f"Note already exists: {note_path}")

    # Build content
    fm_str = _format_frontmatter(with_type(frontmatter))
    content = f"{fm_str}\n\n{body_markdown}"

    note_path.parent.mkdir(parents=True, exist_ok=True)
    # Use a private temporary file and an exclusive link: concurrent creates
    # must never overwrite an existing, potentially human-edited note.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=note_path.parent,
                                     prefix=".claude-note-", delete=False) as temp:
        temp_path = Path(temp.name)
        temp.write(content)
    try:
        os.link(temp_path, note_path)
    finally:
        temp_path.unlink(missing_ok=True)

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

    try:
        note_path = resolve_note_path(op.path, vault_root)
    except ValueError as exc:
        logger.warning("Rejected note operation: %s", exc)
        return False
    if op.managed_block_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", op.managed_block_id):
        logger.warning("Rejected invalid managed block ID: %r", op.managed_block_id)
        return False

    if op.op == "create":
        block_id = op.managed_block_id
        if not block_id:
            identity = session_id or op.body_markdown
            block_id = "synth-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        if note_path.exists():
            # Note exists - use managed block for clean updates
            return managed_blocks.write_managed_block(
                note_path,
                block_id,
                op.body_markdown,
                create_if_missing=True,
            )

        frontmatter = provenance.stamp(with_type(op.frontmatter or {"tags": ["claude-note"]}), assistant)
        content = (managed_blocks._make_start_marker(block_id) + "\n" + op.body_markdown
                   + "\n" + managed_blocks._make_end_marker(block_id))
        create_note(op.path, frontmatter, content, vault_root)
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
        identity = json.dumps([session_id or "", op.path, section, op.body_markdown], ensure_ascii=False)
        block_id = "append-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        if managed_blocks.read_managed_block(note_path, block_id) is not None:
            return True
        content = (managed_blocks._make_start_marker(block_id) + "\n" + op.body_markdown
                   + "\n" + managed_blocks._make_end_marker(block_id))
        return managed_blocks.append_to_section(
            note_path,
            section,
            content,
            create_section=True,
        )

    return False


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
    collection = getattr(config, "QMD_COLLECTION", "")
    if not config.QMD_SYNTH_ENABLED or not config.QMD_LINK_ENHANCE_ENABLED or not collection:
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
            if config.QMD_SEARCH_MODE == "vector":
                results = qmd_search.search_vector(query, limit=5, min_score=min_score, collection=collection)
            else:
                results = qmd_search.search_keyword(qmd_search.keyword_query(query), limit=5, collection=collection)

            # Get existing links as a set for deduplication
            existing_links = set(concept.links_suggested or [])
            added_count = 0

            for result in results:
                path = qmd_search.resolve_result_path(result.path, config.VAULT_ROOT, collection)
                if not path or not path.is_file():
                    continue
                note_name = path.relative_to(config.VAULT_ROOT.resolve()).with_suffix("").as_posix()

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
            concept.links_suggested = sorted(existing_links)

            if added_count > 0:
                logger.debug(f"Enhanced '{concept.name}' with {added_count} semantic links")

    except Exception as e:
        # Silent fallback - don't break routing
        logger = logging.getLogger("claude-note")
        logger.debug(f"Link enhancement failed: {e}")


def _extraction_fingerprint(pack: knowledge_pack.KnowledgePack) -> str:
    """Identify exact extraction content, never merely a similar subject."""
    content = pack.to_dict()
    for key in ("session_id", "date", "time", "title", "assistant"):
        content.pop(key, None)
    payload = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    lines.append(f"<!-- claude-note:extraction:{_extraction_fingerprint(pack)} -->")
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
    Skips append only when the exact extracted content was already captured.
    Related topics and corrections remain independent evidence.

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
        marker = f"<!-- claude-note:extraction:{_extraction_fingerprint(pack)} -->"
        if inbox_path.exists() and marker in inbox_path.read_text(encoding="utf-8"):
            logger.info("Skipping exact duplicate inbox extraction: %s", pack.title)
            return None

    entry = format_inbox_entry(pack)

    if not inbox_path.exists():
        # Create new inbox
        header = """---
type: meta
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
            try:
                inbox_relative = config.INBOX_PATH.relative_to(config.VAULT_ROOT)
            except ValueError:
                inbox_relative = Path(config.INBOX_PATH.name)
            inbox_result = append_to_inbox(pack, Path(vault_root) / inbox_relative)
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
                existed = resolve_note_path(op.path, vault_root).exists()
                # Pass session_id for auto-generating managed block IDs
                success = apply_note_op(op, vault_root, session_id=pack.session_id,
                                        assistant=pack.assistant)
                if success:
                    if op.op == "create" and not existed:
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
