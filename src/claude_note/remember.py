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
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import recall as recall_mod

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
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_note(vault: Path, text: str, *, note_type: str = "gotcha", title: str = "",
               tags: Optional[list[str]] = None, why: str = "", how: str = "",
               folder: str = "", assistant: str = "", now: Optional[datetime] = None) -> tuple[Path, bool]:
    """Create or append. Returns (path, created)."""
    if note_type not in TYPES:
        raise ValueError(f"type must be one of: {', '.join(TYPES)}")
    if not text.strip():
        raise ValueError("nothing to remember")
    now = now or datetime.now()
    title = title.strip() or derive_title(text)
    tags = [t.strip().lstrip("#") for t in (tags or []) if t.strip()]
    assistant = assistant or detect_assistant()
    root = vault.resolve()
    target_dir = (root / folder).resolve() if folder else root
    target_dir.relative_to(root)  # ValueError on traversal
    if any(part.startswith(".") for part in target_dir.relative_to(root).parts):
        raise ValueError("refusing to write into a hidden folder")
    path = target_dir / f"{slugify(title)}.md"

    body = [text.strip()]
    if why.strip():
        body += ["", f"**Why:** {why.strip()}"]
    if how.strip():
        body += ["", f"**How to apply:** {how.strip()}"]
    stamp = now.strftime("%Y-%m-%d")
    block_id = f"remember-{now.strftime('%Y-%m-%d-%H%M%S')}"

    if path.exists():
        existing = path.read_text(encoding="utf-8")
        block = "\n".join([
            "", f"<!-- claude-note:{block_id}:start -->",
            f"## Update {stamp} ({assistant})", "", *body,
            f"<!-- claude-note:{block_id}:end -->", ""])
        path.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8")
        return path, False

    try:
        from .provenance import author_email
        author = author_email()
    except Exception:
        author = ""
    fm = ["---", f"title: {_yaml_str(title)}", f"type: {note_type}", f"created: {stamp}",
          f"assistant: {assistant}"]
    if author:
        fm.append(f"author: {author}")
    if tags:
        fm.append("tags: [" + ", ".join(tags) + "]")
    fm.append("---")
    content = "\n".join(fm + ["", f"# {title}", "", *body, ""])
    target_dir.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:  # exclusive: never clobber a race
        fh.write(content)
    return path, True


def reindex(index: str = "", timeout: float = 120.0) -> tuple[bool, float]:
    """Run `qmd update` so the note is searchable now (BM25 needs no model)."""
    settings = recall_mod.Settings.load()
    exe = recall_mod.qmd_executable(settings)
    if not exe:
        return False, 0.0
    cmd = [exe] + (["--index", index] if index else []) + ["update"]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        return proc.returncode == 0, time.monotonic() - started
    except (OSError, subprocess.TimeoutExpired):
        return False, time.monotonic() - started


def cmd_remember(args) -> int:
    from . import config  # needs a configured vault
    text = " ".join(args.text).strip()
    if text == "-":
        import sys
        text = sys.stdin.read()
    try:
        path, created = write_note(
            config.VAULT_ROOT, text, note_type=args.type, title=args.title or "",
            tags=(args.tags.split(",") if args.tags else []), why=args.why or "",
            how=args.how or "", folder=args.folder or "")
    except (ValueError, OSError) as exc:
        print(f"claude-note remember: {exc}")
        return 1
    rel = path.relative_to(config.VAULT_ROOT.resolve()) if path.is_relative_to(config.VAULT_ROOT.resolve()) else path
    print(f"{'Created' if created else 'Appended to'} {rel}  (type: {args.type})")
    if args.no_index:
        print("Not reindexed (--no-index); recall finds it after the next `qmd update`.")
        return 0
    ok, secs = reindex(recall_mod.Settings.load().index)
    if ok:
        print(f"Indexed in {secs:.1f}s: the next prompt can recall it.")
    else:
        print("Could not run `qmd update`; the note is saved, recall finds it after the next index refresh.")
    return 0
