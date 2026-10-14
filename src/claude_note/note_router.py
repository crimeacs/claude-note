"""
Note router for claude-note synthesizer.

Applies note operations from KnowledgePack to the vault.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

from . import config
from . import managed_blocks
from . import knowledge_pack
from . import qmd_search
from . import provenance
from . import mutation


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
    return mutation.resolve_note_path(path, vault_root)


def _format_frontmatter(fm: dict) -> str:
    return mutation.serialize_frontmatter(fm)


def create_note(path: str, frontmatter: dict, body_markdown: str, vault_root: Path = None) -> Path:
    vault_root = vault_root or config.VAULT_ROOT
    target = resolve_note_path(path, vault_root)
    content = f"{_format_frontmatter(with_type(frontmatter))}\n\n{body_markdown}"
    return mutation.create_note(target, content, vault_root=vault_root).path


def _apply_note_op_result(op, vault_root=None, session_id=None, assistant=''):
    vault_root = vault_root or config.VAULT_ROOT
    try:
        target = resolve_note_path(op.path, vault_root)
    except ValueError as exc:
        logger.warning("Rejected note operation: %s", exc)
        return None
    if op.managed_block_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", op.managed_block_id):
        logger.warning("Rejected invalid managed block ID: %r", op.managed_block_id)
        return None
    expected = getattr(op, 'expected_revision', None)
    if op.op == 'create':
        block_id = op.managed_block_id or 'synth-' + hashlib.sha256((session_id or op.body_markdown).encode()).hexdigest()[:32]
        identity = getattr(op, 'operation_id', '') or mutation.operation_id('create', session_id, target.name, block_id, op.body_markdown)
        if not target.exists():
            frontmatter = provenance.stamp(with_type(op.frontmatter or {'tags': ['claude-note']}), assistant)
            block = f"{managed_blocks._make_start_marker(block_id)}\n{op.body_markdown}\n{managed_blocks._make_end_marker(block_id)}"
            content = f"{_format_frontmatter(frontmatter)}\n\n{block}"
            try:
                return mutation.create_note(target, content, vault_root=vault_root, operation_id=identity)
            except FileExistsError:
                pass  # Another writer won the exclusive create; inspect under its lock.
        return managed_blocks.write_managed_block_result(target, block_id, op.body_markdown, True,
                    vault_root=vault_root, expected_revision=expected, operation_id=identity)
    if op.op == 'upsert_block':
        if not target.exists(): return None
        return managed_blocks.write_managed_block_result(target, op.managed_block_id or 'synth',
                    op.body_markdown, True, vault_root=vault_root, expected_revision=expected,
                    operation_id=getattr(op, 'operation_id', '') or None)
    if op.op == 'append':
        if not target.exists(): return None
        section = op.section or '## Synthesized'
        identity = getattr(op, 'operation_id', '') or mutation.operation_id('append', session_id, target.relative_to(Path(vault_root).resolve()).as_posix(), section, op.body_markdown)
        block_id = 'append-' + identity[:48]
        content = f"{managed_blocks._make_start_marker(block_id)}\n{op.body_markdown}\n{managed_blocks._make_end_marker(block_id)}"
        return managed_blocks.append_to_section_result(target, section, content, True,
                                                       vault_root=vault_root, operation_id=identity)
    return None


def apply_note_op(op: knowledge_pack.NoteOp, vault_root: Path = None, session_id: str = None,
                  assistant: str = '') -> bool:
    """Legacy boolean API: staged proposals are NOT reported as applied writes."""
    result = _apply_note_op_result(op, vault_root, session_id, assistant)
    if result is not None and not result.applied:
        logger.warning("Note operation staged: %s (%s)", result.staged_path, result.reason)
    return result is not None and result.applied


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
    if pack.generation and pack.source_revision:
        return mutation.operation_id('extraction-generation', pack.session_id, pack.generation, pack.source_revision)
    content = pack.to_dict()
    for key in ("session_id", "date", "time", "title", "assistant"):
        content.pop(key, None)
    for concept in content.get('concepts', []):
        concept.pop('links_suggested', None)  # post-extraction routing enrichment
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


def append_to_inbox(pack: knowledge_pack.KnowledgePack, inbox_path: Path = None,
                    skip_dedup: bool = False, *, vault_root: Path = None) -> Optional[Path]:
    inbox_path = inbox_path or config.INBOX_PATH
    vault_root = vault_root or config.VAULT_ROOT
    marker = f"<!-- claude-note:extraction:{_extraction_fingerprint(pack)} -->"
    entry = format_inbox_entry(pack)
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
    def transform(current):
        if not skip_dedup and config.INBOX_DEDUP_ENABLED and current and marker in current:
            return None
        if current is None: return header + entry
        separator = "\n---\n\n"
        first = current.find(separator)
        second = current.find(separator, first + 1) if first >= 0 else -1
        if second >= 0:
            position = second + len(separator)
            return current[:position] + entry + current[position:]
        if current.startswith('---'):
            end = current.find('\n---\n', 3)
            if end >= 0: return current[:end + 5] + '\n' + entry + current[end + 5:]
        return entry + current
    identity = mutation.operation_id('inbox', _extraction_fingerprint(pack)) if not skip_dedup else None
    result = mutation.mutate(inbox_path, transform, vault_root=vault_root,
                             operation_id=identity, allow_protected=True)
    if not result.applied:
        raise mutation.MutationConflict(result)
    return None if result.status == 'unchanged' else result.path


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
        "notes_staged": [],
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
            inbox_result = append_to_inbox(pack, Path(vault_root) / inbox_relative, vault_root=vault_root)
            if inbox_result is not None:
                results["inbox_updated"] = True
            # If None, it was skipped due to deduplication (not an error)
        except mutation.MutationConflict as exc:
            results['notes_staged'].append({'path': str(exc.result.path), 'pending_path': str(exc.result.staged_path), 'reason': exc.result.reason})
        except Exception as e:
            results["errors"].append(f"Inbox update failed: {e}")

    # In inbox mode, we're done
    if mode == "inbox":
        return results

    # In route mode, apply note_ops
    if mode == "route":
        for claim in pack.claims:
            try:
                claim.validate()
                path = Path("claims") / (claim.claim_id + ".md")
                content = (mutation.serialize_frontmatter({"type": "reference", "verification": "proposed",
                    "source_revision": claim.source_revision}) + "\n\n# " + claim.claim_id
                    + "\n\n" + claim.marker() + "\n")
                outcome = mutation.mutate(path, lambda previous: content, vault_root=vault_root,
                    operation_id=mutation.operation_id("claim", claim.claim_id, claim.digest()))
                if outcome.applied:
                    if outcome.status == "created":
                        results["notes_created"].append(str(path))
                else:
                    results["notes_staged"].append({"path": str(path), "pending_path": str(outcome.staged_path), "reason": outcome.reason})
            except Exception as exc:
                results["errors"].append(f"Claim persistence failed: {exc}")
        for op in pack.note_ops:
            try:
                existed = resolve_note_path(op.path, vault_root).exists()
                # Pass session_id for auto-generating managed block IDs
                outcome = _apply_note_op_result(op, vault_root, session_id=pack.session_id,
                                                assistant=pack.assistant)
                if outcome is not None and not outcome.applied:
                    results['notes_staged'].append({'path': op.path, 'pending_path': str(outcome.staged_path), 'reason': outcome.reason})
                elif outcome is not None:
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
