"""
Where a note came from: which assistant produced the session, and who wrote it.

Every note claude-note writes carries two frontmatter fields:

  assistant: claude-code | codex | cursor | claude-app | chatgpt
  author:    the owner's email

The daily push forwards both, so the shared vault can say which assistant a
learning came from without guessing.

This module must not import `config`: `claude-note status --json` uses it on a
machine where claude-note is not configured yet.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

ASSISTANTS = ("claude-code", "codex", "cursor", "claude-app", "chatgpt")

# Importer source name (importers.write_session) -> assistant.
_IMPORTER_SOURCES = {
    "cursor": "cursor",
    "claude-ai": "claude-app",
    "chatgpt": "chatgpt",
    "codex": "codex",
    "claude-code": "claude-code",
}

FORESYN_CONFIG = Path.home() / ".foresyn/config.json"


def assistant_for(transcript_path: str = "", source: Optional[str] = None) -> str:
    """The assistant a session came from, from its importer source or transcript path.

    Transcript locations:
      ~/.claude/projects/...                          -> claude-code (hook)
      ~/.codex/sessions/.../rollout-*.jsonl           -> codex (hook or import; the
                                                         ChatGPT desktop app runs Codex)
      ~/.local/share/claude-note/transcripts/<src>/   -> the importer's source
    """
    if source and source in _IMPORTER_SOURCES:
        return _IMPORTER_SOURCES[source]
    path = (transcript_path or "").replace("\\", "/")
    if "/.codex/" in path:
        return "codex"
    marker = "/claude-note/transcripts/"
    if marker in path:
        sub = path.split(marker, 1)[1].split("/", 1)[0]
        if sub in _IMPORTER_SOURCES:
            return _IMPORTER_SOURCES[sub]
    if "/.claude/" in path:
        return "claude-code"
    # A hook with no transcript path is still a Claude Code hook: it is the only
    # producer that does not go through a known transcript location.
    return "claude-code" if not path else ""


def _config_toml() -> dict:
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = Path(xdg) / "claude-note" / "config.toml"
    if not path.exists() or sys.version_info < (3, 11):
        return {}
    import tomllib
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


def _foresyn_email() -> str:
    try:
        cfg = json.loads(FORESYN_CONFIG.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(cfg, dict):
        return ""
    for key in ("email", "userEmail", "user_email"):
        value = cfg.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
    user = cfg.get("user")
    if isinstance(user, dict) and isinstance(user.get("email"), str):
        return user["email"].strip()
    return ""


def _git_email() -> str:
    try:
        return subprocess.run(["git", "config", "--global", "user.email"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


_author_cache: Optional[str] = None


def author_email() -> str:
    """The owner's email. First hit wins:

    CLAUDE_NOTE_AUTHOR, `author` in config.toml (the Sweat app writes it from
    the signed-in workspace), the email in ~/.foresyn/config.json, then the
    global git user.email.
    """
    global _author_cache
    if _author_cache is None:
        _author_cache = (os.environ.get("CLAUDE_NOTE_AUTHOR", "").strip()
                         or str(_config_toml().get("author", "")).strip()
                         or _foresyn_email()
                         or _git_email())
    return _author_cache


def stamp(frontmatter: dict, assistant: str = "") -> dict:
    """A copy of `frontmatter` with `assistant` and `author` added when absent."""
    out = dict(frontmatter)
    if assistant and not out.get("assistant"):
        out["assistant"] = assistant
    author = author_email()
    if author and not out.get("author"):
        out["author"] = author
    return out
