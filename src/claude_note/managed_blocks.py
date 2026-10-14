"""Managed blocks with confined append and explicit replacement conflicts.

Missing blocks can be appended without rewriting human prose. Replacing or
removing a block requires an explicit base revision and machine-owned editing
contract; otherwise an immutable review proposal is saved and False returned.
"""
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union

from . import mutation

BLOCK_START_PATTERN = re.compile(r"<!--\s*claude-note:([^:]+):start\s*-->")
BLOCK_END_PATTERN = re.compile(r"<!--\s*claude-note:([^:]+):end\s*-->")
logger = logging.getLogger(__name__)


def _make_start_marker(block_id: str) -> str:
    return f"<!-- claude-note:{block_id}:start -->"


def _make_end_marker(block_id: str) -> str:
    return f"<!-- claude-note:{block_id}:end -->"


def _validate_id(block_id):
    if not isinstance(block_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", block_id):
        raise ValueError("Invalid managed block identity")


@contextmanager
def _note_lock(note_path: Path, timeout: float = 30.0, *, vault_root=None):
    with mutation.note_lock(note_path, vault_root=vault_root, timeout=timeout):
        yield


def _atomic_write(path: Path, content: str, *, vault_root=None, expected_revision=None):
    result = mutation.mutate(path, lambda old: content, vault_root=vault_root,
                             expected_revision=expected_revision)
    if not result.applied:
        raise mutation.MutationConflict(result)


def _locate(text, block_id):
    starts = list(re.finditer(r'<!--\s*claude-note:' + re.escape(block_id) + r':start\s*-->', text))
    ends = list(re.finditer(r'<!--\s*claude-note:' + re.escape(block_id) + r':end\s*-->', text))
    if len(starts) > 1 or len(ends) != len(starts):
        raise ValueError("Duplicate or malformed managed block; review before updating")
    if not starts:
        return None
    start, end = starts[0], ends[0]
    if end.start() < start.end():
        raise ValueError("Malformed managed block")
    return start.start(), end.end(), text[start.end():end.start()].strip()


def _validate_layout(text):
    events = [(m.start(), 'start', m[1]) for m in BLOCK_START_PATTERN.finditer(text)]
    events.extend((m.start(), 'end', m[1]) for m in BLOCK_END_PATTERN.finditer(text))
    active, seen = None, set()
    for _, kind, identity in sorted(events):
        _validate_id(identity)
        if kind == 'start':
            if active is not None or identity in seen:
                raise ValueError('Nested or duplicate managed markers')
            active = identity
            seen.add(identity)
        else:
            if active != identity:
                raise ValueError('Mismatched managed markers')
            active = None
    if active is not None:
        raise ValueError('Unclosed managed block')


def read_managed_block(note_path: Union[Path, str], block_id: str, *, vault_root=None) -> Optional[str]:
    _validate_id(block_id)
    text = mutation.read_text(Path(note_path), vault_root=vault_root)
    if text is None:
        return None
    located = _locate(text, block_id)
    return located[2] if located else None


def write_managed_block_result(note_path, block_id, content, create_if_missing=False, *,
                               vault_root=None, expected_revision=None, operation_id=None):
    _validate_id(block_id)
    block = f"{_make_start_marker(block_id)}\n{content}\n{_make_end_marker(block_id)}"
    _validate_layout(block)
    def transform(text):
        if text is None:
            return None
        try:
            _validate_layout(text)
        except ValueError as error:
            return mutation.MutationProposal(text + '\n' + block + '\n', str(error) + '; review required')
        located = _locate(text, block_id)
        if located:
            begin, end, existing = located
            if existing == content.strip():
                return None
            return text[:begin] + block + text[end:]
        if create_if_missing:
            return text + ("" if text.endswith("\n") else "\n") + block + "\n"
        return None
    return mutation.mutate(Path(note_path), transform, vault_root=vault_root,
                           operation_id=operation_id or mutation.operation_id('block', block_id, content),
                           expected_revision=expected_revision, require_revision=True)


def _accepted(result):
    if not result.applied:
        logger.warning("Mutation staged for review: %s (%s)", result.staged_path, result.reason)
    return result.applied


def write_managed_block(note_path, block_id, content, create_if_missing=False, *,
                        vault_root=None, expected_revision=None, operation_id=None) -> bool:
    result = write_managed_block_result(note_path, block_id, content, create_if_missing,
                                        vault_root=vault_root, expected_revision=expected_revision,
                                        operation_id=operation_id)
    # Preserve the legacy missing-note/missing-block False contract.
    if result.status == 'unchanged' and mutation.read_text(Path(note_path), vault_root=vault_root) is None:
        return False
    if result.status == 'unchanged' and not create_if_missing:
        return read_managed_block(note_path, block_id, vault_root=vault_root) is not None
    return _accepted(result)


def delete_managed_block(note_path, block_id, *, vault_root=None, expected_revision=None) -> bool:
    _validate_id(block_id)
    found = False
    def transform(text):
        nonlocal found
        located = _locate(text, block_id) if text is not None else None
        if not located:
            return None
        found = True
        begin, end, _ = located
        if end < len(text) and text[end] == "\n": end += 1
        if begin > 0 and text[begin - 1] == "\n": begin -= 1
        return text[:begin] + text[end:]
    result = mutation.mutate(Path(note_path), transform, vault_root=vault_root,
                             operation_id=mutation.operation_id('delete-block', block_id, expected_revision),
                             expected_revision=expected_revision, require_revision=True)
    return found and _accepted(result)


def list_managed_blocks(note_path, *, vault_root=None) -> list[str]:
    text = mutation.read_text(Path(note_path), vault_root=vault_root)
    return BLOCK_START_PATTERN.findall(text) if text is not None else []


def append_to_section_result(note_path, section_heading, content, create_section=True, *,
                             vault_root=None, operation_id=None):
    _validate_layout(content)
    def transform(text):
        if text is None:
            return None
        try:
            _validate_layout(text)
        except ValueError as error:
            return mutation.MutationProposal(text + '\n' + content + '\n', str(error) + '; review required')
        # This replay test occurs under the SAME lock as the eventual append.
        ids = BLOCK_START_PATTERN.findall(content)
        addition, present = content, 0
        for identity in ids:
            existing, incoming = _locate(text, identity), _locate(addition, identity)
            if existing:
                if existing[2] != incoming[2]:
                    return mutation.MutationProposal(text + '\n' + content + '\n', 'Managed block identity has changed content')
                addition = addition[:incoming[0]] + addition[incoming[1]:]
                present += 1
        if ids and present == len(ids): return None
        addition = addition.strip('\n') if present else addition
        lines = text.split("\n")
        section = next((i for i, line in enumerate(lines) if line.strip() == section_heading), None)
        if section is None:
            if not create_section: return None
            return text + f"\n{section_heading}\n\n{addition}\n"
        insert = section + 1
        level = len(section_heading) - len(section_heading.lstrip('#'))
        while insert < len(lines):
            heading = re.match(r'^(#{1,6})\s', lines[insert])
            if heading and len(heading.group(1)) <= level: break
            insert += 1
        lines[insert:insert] = ["", addition]
        return "\n".join(lines)
    return mutation.mutate(Path(note_path), transform, vault_root=vault_root,
                           operation_id=operation_id or mutation.operation_id('section', section_heading, content))


def append_to_section(note_path, section_heading, content, create_section=True, *,
                      vault_root=None, operation_id=None) -> bool:
    result = append_to_section_result(note_path, section_heading, content, create_section,
                                      vault_root=vault_root, operation_id=operation_id)
    return mutation.read_text(Path(note_path), vault_root=vault_root) is not None and _accepted(result)


def find_section_content(note_path: Union[Path, str], section_heading: str) -> Optional[str]:
    """
    Find and return the content of a section.

    Args:
        note_path: Path to the note file
        section_heading: Section heading (e.g., "## Summary")

    Returns:
        Section content, or None if section not found
    """
    content = mutation.read_text(Path(note_path))
    if content is None:
        return None
    lines = content.split("\n")

    # Find section
    section_idx = None
    for i, line in enumerate(lines):
        if line.strip() == section_heading:
            section_idx = i
            break

    if section_idx is None:
        return None

    # Find end of section
    end_idx = section_idx + 1
    heading_level = section_heading.count("#")

    while end_idx < len(lines):
        line = lines[end_idx]
        # Check if this is a heading of same or higher level
        if line.startswith("#"):
            line_level = len(line) - len(line.lstrip("#"))
            if line_level <= heading_level:
                break
        end_idx += 1

    # Extract section content
    section_lines = lines[section_idx + 1:end_idx]
    return "\n".join(section_lines).strip()
