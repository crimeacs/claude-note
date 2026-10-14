"""`claude-note remember`: write a lesson down now, findable by the next session.

The worker extracts knowledge from transcripts after a session ends. That is the
safety net. The habit that compounds is an agent (or a person) writing the
lesson the moment it is learned, with its type, then reindexing so the very next
prompt can recall it. A session that learned something and wrote nothing down did
half the work.

A note that already exists is never overwritten: the lesson is appended as a
dated managed block, and recall shows the newest block first.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import recall as recall_mod
from . import mutation

TYPES = ("gotcha", "pattern", "decision", "reference", "feedback", "project")


def detect_assistant() -> str:
    if os.environ.get("CLAUDE_NOTE_ASSISTANT"):
        return os.environ["CLAUDE_NOTE_ASSISTANT"]
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    return "human"


def slugify(text: str, limit: int = 64) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-") or "note")


def derive_title(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s|\n|:\s| -> | — ", text.strip(), maxsplit=1)[0]
    words = first.split()
    return " ".join(words[:10]).rstrip(".,;:") or "Untitled lesson"


def _yaml_str(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def write_note_result(vault: Path, text: str, *, note_type: str = "gotcha", title: str = "",
                      tags: Optional[list[str]] = None, why: str = "", how: str = "",
                      folder: str = "", assistant: str = "", now: Optional[datetime] = None,
                      operation_id: str | None = None) -> mutation.MutationResult:
    """Record a lesson through the common confined, physical-append writer."""
    if note_type not in TYPES:
        raise ValueError(f"type must be one of: {', '.join(TYPES)}")
    if not text.strip():
        raise ValueError("nothing to remember")
    now = now or datetime.now()
    title = title.strip() or derive_title(text)
    tags = [t.strip().lstrip("#") for t in (tags or []) if t.strip()]
    assistant = assistant or detect_assistant()
    relative = (Path(folder) / f"{slugify(title)}.md").as_posix()
    # Do not resolve parent symlinks away before validating the lexical route.
    path = mutation.resolve_note_path(relative, vault)
    relative = path.relative_to(mutation._vault(vault)).as_posix()
    body = [text.strip()]
    if why.strip(): body += ["", f"**Why:** {why.strip()}"]
    if how.strip(): body += ["", f"**How to apply:** {how.strip()}"]
    stamp = now.strftime("%Y-%m-%d")
    op_id = operation_id or mutation.operation_id('remember', relative, note_type, title, tags, body, assistant)
    block_id = 'remember-' + op_id[:48]
    marker = f"<!-- claude-note:{block_id}:start -->"
    try:
        from .provenance import author_email
        author = author_email()
    except Exception:
        author = ''
    fm = {'title': title, 'type': note_type, 'created': stamp, 'assistant': assistant}
    if author: fm['author'] = author
    if tags: fm['tags'] = tags
    frontmatter = mutation.serialize_frontmatter(fm, plain_keys=('type', 'created', 'assistant', 'author'))
    # The initial lesson also carries an identity, allowing recovery after a
    # note commit followed by a crash before its separate effect receipt.
    initial = '\n'.join([frontmatter, '', f'# {title}', '', marker, *body,
                         f'<!-- claude-note:{block_id}:end -->', ''])
    addition = '\n'.join(['', marker, f'## Update {stamp} ({assistant})', '', *body,
                          f'<!-- claude-note:{block_id}:end -->', ''])
    from . import managed_blocks
    managed_blocks._validate_layout(initial)
    managed_blocks._validate_layout(addition)
    body_text = '\n'.join(body).strip()
    def transform(existing):
        if existing is None: return initial
        candidate = existing + ('' if existing.endswith('\n') else '\n') + addition
        try:
            managed_blocks._validate_layout(existing)
            located = managed_blocks._locate(existing, block_id)
        except ValueError as error:
            return mutation.MutationProposal(candidate, str(error) + '; review required')
        if located:
            block_body = located[2]
            appended = re.fullmatch(r'## Update \d{4}-\d{2}-\d{2} \(' + re.escape(assistant)
                                    + r'\)\n\n' + re.escape(body_text), block_body)
            if block_body == body_text or appended:
                return None
            return mutation.MutationProposal(candidate, 'Remember identity has changed content; review required')
        return candidate
    return mutation.mutate(path, transform, vault_root=vault, operation_id=op_id)


def write_note(vault: Path, text: str, **kwargs) -> tuple[Path, bool]:
    """Backward-compatible (path, created), with explicit staged conflicts."""
    result = write_note_result(vault, text, **kwargs)
    if not result.applied:
        raise mutation.MutationConflict(result)
    return result.path, result.status == 'created'


def reindex(index: str = "", timeout: float = 120.0) -> tuple[bool, float]:
    """Index text and acknowledge only the generation captured before it."""
    from . import index_refresh

    settings = recall_mod.Settings.load()
    exe = recall_mod.qmd_executable(settings)
    if not exe:
        return False, 0.0
    cmd = [exe] + (["--index", index] if index else []) + ["update"]
    started = time.monotonic()
    lease = None
    succeeded = False
    try:
        try:
            lease = index_refresh.begin(index=index)
        except (OSError, ValueError) as exc:
            print(f"Index freshness capture unavailable: {type(exc).__name__}", file=sys.stderr)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        succeeded = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        if lease is not None:
            try:
                result = index_refresh.finish(lease['token'], lexical_ok=succeeded)
                if result.get('warning'):
                    print(f"Index freshness: {result['warning']}", file=sys.stderr)
            except (OSError, ValueError) as exc:
                print(f"Index freshness needs reconciliation: {type(exc).__name__}", file=sys.stderr)
    return succeeded, time.monotonic() - started


def cmd_remember(args) -> int:
    from . import config  # needs a configured vault
    text = " ".join(args.text).strip()
    if text == "-":
        import sys
        text = sys.stdin.read()
    try:
        result = write_note_result(
            config.VAULT_ROOT, text, note_type=args.type, title=args.title or "",
            tags=(args.tags.split(",") if args.tags else []), why=args.why or "",
            how=args.how or "", folder=args.folder or "")
    except (ValueError, OSError, mutation.MutationConflict) as exc:
        print(f"claude-note remember: {exc}")
        return 1
    if not result.applied:
        print(f"Staged for review: {result.staged_path} ({result.reason})")
        return 2
    path = result.path
    rel = path.relative_to(config.VAULT_ROOT.resolve()) if path.is_relative_to(config.VAULT_ROOT.resolve()) else path
    verb = {'created': 'Created', 'updated': 'Appended to', 'unchanged': 'Already recorded in'}[result.status]
    print(f"{verb} {rel}  (type: {args.type})")
    if args.no_index:
        print("Not reindexed (--no-index); recall finds it after the next `qmd update`.")
        return 0
    ok, secs = reindex(recall_mod.Settings.load().index)
    if ok:
        print(f"Indexed in {secs:.1f}s: the next prompt can recall it.")
    else:
        print("Could not run `qmd update`; the note is saved, recall finds it after the next index refresh.")
    return 0
