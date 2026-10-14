"""
Vault indexer for claude-note synthesizer.

Builds and maintains an index of vault notes for linking and context.
"""

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from . import config


@dataclass
class NoteIndex:
    """Index entry for a single note."""
    path: str                              # Relative to vault root
    title: str                             # H1 or filename
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    outbound_links: list[str] = field(default_factory=list)
    preview: str = ""                      # First ~200 chars of body
    mtime: float = 0.0                     # For cache invalidation

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NoteIndex":
        return cls(**data)


@dataclass
class VaultIndex:
    """Complete vault index."""
    notes: dict[str, NoteIndex] = field(default_factory=dict)  # path -> NoteIndex
    last_full_scan: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            "notes": {k: v.to_dict() for k, v in self.notes.items()},
            "last_full_scan": self.last_full_scan,
        }, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "VaultIndex":
        data = json.loads(json_str)
        notes = {
            k: NoteIndex.from_dict(v)
            for k, v in data.get("notes", {}).items()
        }
        return cls(
            notes=notes,
            last_full_scan=data.get("last_full_scan", 0.0),
        )


# Regex patterns for parsing
FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
WIKILINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
TAG_PATTERN = re.compile(r"(?:^|\s)#([\w/-]+)")
H1_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """
    Parse YAML frontmatter from note content.

    Returns (frontmatter_dict, body_content).
    """
    match = FRONTMATTER_PATTERN.match(content)
    if not match:
        return {}, content

    yaml_str = match.group(1)
    body = content[match.end():]

    # Simple YAML parsing (no external deps)
    frontmatter = {}
    current_key = None
    current_list = None

    for line in yaml_str.split("\n"):
        line = line.rstrip()

        # List item
        if line.startswith("  - ") and current_key:
            if current_list is None:
                current_list = []
                frontmatter[current_key] = current_list
            current_list.append(line[4:].strip().strip('"\''))
            continue

        # Key-value pair
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"\'')

            current_key = key
            current_list = None

            if value:
                # Check if it's a list in flow style [a, b, c]
                if value.startswith("[") and value.endswith("]"):
                    items = [
                        item.strip().strip('"\'')
                        for item in value[1:-1].split(",")
                        if item.strip()
                    ]
                    frontmatter[key] = items
                else:
                    frontmatter[key] = value

    return frontmatter, body


def _extract_title(content: str, filename: str) -> str:
    """Extract title from note content."""
    # Try to find H1
    match = H1_PATTERN.search(content)
    if match:
        return match.group(1).strip()

    # Fall back to filename without extension
    return Path(filename).stem


def _extract_preview(body: str, max_len: int = 200) -> str:
    """Extract preview text from note body."""
    # Skip empty lines and headers
    lines = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("**") and line.endswith("**"):
            continue
        lines.append(line)
        if len(" ".join(lines)) > max_len:
            break

    preview = " ".join(lines)
    if len(preview) > max_len:
        preview = preview[:max_len] + "..."
    return preview


def _extract_links(content: str) -> list[str]:
    """Extract wiki links from content."""
    matches = WIKILINK_PATTERN.findall(content)
    # Dedupe while preserving order
    seen = set()
    links = []
    for link in matches:
        link = link.strip()
        if link and link not in seen:
            links.append(link)
            seen.add(link)
    return links


def _extract_inline_tags(content: str) -> list[str]:
    """Extract inline #tags from content."""
    matches = TAG_PATTERN.findall(content)
    seen = set()
    tags = []
    for tag in matches:
        if tag and tag not in seen:
            tags.append(tag)
            seen.add(tag)
    return tags


def index_note(note_path: Path, vault_root: Path) -> NoteIndex:
    """
    Build index entry for a single note.

    Args:
        note_path: Absolute path to the note
        vault_root: Vault root directory

    Returns:
        NoteIndex for the note
    """
    rel_path = str(note_path.relative_to(vault_root))
    mtime = note_path.stat().st_mtime

    try:
        content = note_path.read_text(encoding="utf-8")
    except Exception:
        return NoteIndex(path=rel_path, title=note_path.stem, mtime=mtime)

    frontmatter, body = _parse_frontmatter(content)

    # Extract metadata
    title = _extract_title(body, note_path.name)

    # Tags from frontmatter + inline
    fm_tags = frontmatter.get("tags", [])
    if isinstance(fm_tags, str):
        fm_tags = [fm_tags]
    inline_tags = _extract_inline_tags(body)
    tags = list(dict.fromkeys(fm_tags + inline_tags))  # Dedupe, preserve order

    # Aliases from frontmatter
    aliases = frontmatter.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]

    # Outbound links
    links = _extract_links(content)

    # Preview
    preview = _extract_preview(body)

    return NoteIndex(
        path=rel_path,
        title=title,
        tags=tags,
        aliases=aliases,
        outbound_links=links,
        preview=preview,
        mtime=mtime,
    )


def build_index(vault_root: Path = None) -> VaultIndex:
    """
    Build complete vault index by scanning all markdown files.

    Args:
        vault_root: Override vault root (defaults to config.VAULT_ROOT)

    Returns:
        VaultIndex with all notes
    """
    if vault_root is None:
        vault_root = config.VAULT_ROOT

    index = VaultIndex(last_full_scan=time.time())

    # Find all markdown files (excluding hidden dirs and templates)
    for md_file in vault_root.glob("**/*.md"):
        # Skip hidden directories
        parts = md_file.relative_to(vault_root).parts
        if any(p.startswith(".") for p in parts):
            continue

        # Skip templates directory
        if "templates" in parts:
            continue

        try:
            note_index = index_note(md_file, vault_root)
            index.notes[note_index.path] = note_index
        except Exception:
            continue

    return index


def load_index() -> Optional[VaultIndex]:
    """Load cached vault index from disk."""
    index_path = config.INDEX_PATH

    if not index_path.exists():
        return None

    try:
        from . import mutation
        content = mutation.read_text(index_path, vault_root=config.VAULT_ROOT)
        return VaultIndex.from_json(content) if content is not None else None
    except Exception:
        return None


def save_index(index: VaultIndex) -> None:
    """Save vault index to disk."""
    index_path = config.INDEX_PATH

    from . import mutation
    content = index.to_json()
    existing = mutation.read_text(index_path, vault_root=config.VAULT_ROOT)
    result = mutation.mutate(index_path, lambda before: content,
                             vault_root=config.VAULT_ROOT, allow_protected=True,
                             expected_revision=mutation.revision(existing) if existing is not None else None,
                             machine_owned=True,
                             operation_id=mutation.operation_id('vault-index', content))
    if not result.applied:
        raise mutation.MutationConflict(result)


def update_index(changed_files: list[Path] = None) -> VaultIndex:
    """
    Update vault index incrementally.

    Args:
        changed_files: List of changed files to re-index.
                      If None, rebuilds entire index.

    Returns:
        Updated VaultIndex
    """
    vault_root = config.VAULT_ROOT

    # Load existing index or build new one
    index = load_index()
    if index is None or changed_files is None:
        index = build_index()
        save_index(index)
        return index

    # Incremental update
    for file_path in changed_files:
        if not file_path.exists():
            # File deleted
            rel_path = str(file_path.relative_to(vault_root))
            if rel_path in index.notes:
                del index.notes[rel_path]
        elif file_path.suffix == ".md":
            # File added/modified
            try:
                note_index = index_note(file_path, vault_root)
                index.notes[note_index.path] = note_index
            except Exception:
                pass

    index.last_full_scan = time.time()
    save_index(index)
    return index


def get_index(rebuild_if_stale: bool = True) -> VaultIndex:
    """
    Get vault index, rebuilding if necessary.

    Args:
        rebuild_if_stale: If True, rebuild if older than INDEX_REFRESH_INTERVAL

    Returns:
        VaultIndex
    """
    index = load_index()
    if index is None:
        index = build_index()
        save_index(index)
        return index

    # Check staleness
    if rebuild_if_stale:
        age = time.time() - index.last_full_scan
        if age > config.INDEX_REFRESH_INTERVAL:
            index = build_index()
            save_index(index)

    return index


def find_related(
    keywords: list[str] = None,
    tags: list[str] = None,
    limit: int = 10,
) -> list[NoteIndex]:
    """
    Find notes related to given keywords and tags.

    Uses simple scoring:
    1. Exact filename match (highest)
    2. Alias match
    3. Tag overlap
    4. Keyword in title/preview

    Args:
        keywords: List of keywords to search for
        tags: List of tags to match
        limit: Maximum number of results

    Returns:
        List of NoteIndex sorted by relevance
    """
    index = get_index()
    keywords = keywords or []
    tags = tags or []

    # Normalize keywords to lowercase
    keywords = [k.lower() for k in keywords]

    scored = []

    for path, note in index.notes.items():
        score = 0

        # Filename match
        filename_lower = Path(path).stem.lower()
        for kw in keywords:
            if kw == filename_lower:
                score += 100
            elif kw in filename_lower:
                score += 50

        # Alias match
        for alias in note.aliases:
            alias_lower = alias.lower()
            for kw in keywords:
                if kw == alias_lower:
                    score += 80
                elif kw in alias_lower:
                    score += 40

        # Tag overlap
        note_tags = set(note.tags)
        tag_matches = len(note_tags.intersection(tags))
        score += tag_matches * 30

        # Keyword in title
        title_lower = note.title.lower()
        for kw in keywords:
            if kw in title_lower:
                score += 20

        # Keyword in preview
        preview_lower = note.preview.lower()
        for kw in keywords:
            if kw in preview_lower:
                score += 5

        if score > 0:
            scored.append((score, note))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    return [note for score, note in scored[:limit]]


def get_all_tags() -> list[str]:
    """Get all unique tags in the vault."""
    index = get_index()
    tags = set()
    for note in index.notes.values():
        tags.update(note.tags)
    return sorted(tags)


def get_notes_by_tag(tag: str) -> list[NoteIndex]:
    """Get all notes with a specific tag."""
    index = get_index()
    return [
        note for note in index.notes.values()
        if tag in note.tags
    ]


def get_index_summary() -> dict:
    """Get summary statistics of the vault index."""
    index = get_index()

    tag_counts = {}
    for note in index.notes.values():
        for tag in note.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    return {
        "total_notes": len(index.notes),
        "unique_tags": len(tag_counts),
        "top_tags": sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10],
        "last_scan": index.last_full_scan,
    }
