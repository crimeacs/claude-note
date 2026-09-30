"""
Daily push of curated notes to the shared Foresyn vault.

Only knowledge notes leave the laptop: frontmatter `type` in PUSH_TYPES, not
`share: false`, not under a `private/` folder. Session notes (raw transcripts)
never do. Every note is redacted before upload; a note the scanner cannot
check is skipped for the run. Unchanged notes (same sha as the last push of
that source path) cause no request at all.

Contract with the server-side merge (keep exact):
  PUT {baseUrl}/api/v1/vault/document?organization_id=<org>
  body {path, content, parent_revision, metadata}
  path = inbox/<person>/<YYYY-MM-DD>/<relative path, "/" -> "__">.md
  metadata = {source, author, assistant, laptop, source_path, content_sha256, note_type, redactions}
  (author: the owner's email, else the note's `author:` frontmatter; assistant:
  the note's `assistant:` frontmatter, "" for notes that predate it)
"""

import getpass
import hashlib
import json
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

from . import config
from . import provenance

PUSH_TYPES = {"pattern", "gotcha", "decision", "reference", "project", "literature"}
STATE_FILE = Path.home() / ".local/share/claude-note/push-state.json"
HEARTBEAT = Path.home() / "Library/Logs/claude-note/push.ok"
FORESYN_CONFIG = Path.home() / ".foresyn/config.json"
USER_AGENT = "claude-note-push/1.0 (+https://github.com/crimeacs/claude-note)"
REQUEST_DEADLINE = 60        # wall-clock seconds per HTTP request
RUN_DEADLINE = 45 * 60       # wall-clock seconds for the whole run
RETRY_DELAY = 5              # seconds before the single retry of a transient failure
DETERMINISTIC = (401, 403, 429)

# Well-known secret shapes (subset of the gitleaks / detect-secrets rules).
# Order matters: specific rules first, generic assignment last.
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("AWS key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("Stripe key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("Supabase key", re.compile(r"\bsb_(?:secret|publishable)_[A-Za-z0-9_\-]{20,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("connection string", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]{3,}@[^\s/]+")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{20,}")),
    ("secret", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|_key)[A-Z0-9_]*)"
        r"(\s*[:=]\s*[\"']?)(?![\"']?(?:\[|<|\{|\(|\$|\*\*|\.\.\.|x{6}|(?:os|process|env|config|settings|self|args|req|request)\.))([^\s\"'`,;]{8,})")),
]


def redact(text: str) -> tuple[str, int]:
    """Replace secrets with [REDACTED: <kind>]. Returns (text, count)."""
    total = 0
    for label, pattern in SECRET_PATTERNS:
        if label == "secret":
            text, n = pattern.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED: {label}]", text)
        elif label == "connection string":
            text, n = pattern.subn(lambda m: re.sub(r"://[^@]+@", f"://[REDACTED: {label}]@", m.group(0)), text)
        else:
            text, n = pattern.subn(f"[REDACTED: {label}]", text)
        total += n
    return text, total


def frontmatter(text: str) -> dict:
    """Top-level scalar keys of a YAML frontmatter block (no YAML dependency)."""
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        kv = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            out[kv.group(1)] = kv.group(2).strip().strip("\"'")
    return out


def eligible(rel: Path, text: str) -> Optional[str]:
    """The note type if this note may leave the laptop, else None."""
    if any(p.startswith(".") or p == "private" for p in rel.parts[:-1]):
        return None
    fm = frontmatter(text)
    note_type = fm.get("type", "").lower()
    if note_type not in PUSH_TYPES or fm.get("share", "").lower() == "false":
        return None
    return note_type


STUB_MIN_CHARS = 200
_PLACEHOLDER = re.compile(r"(?i)never written up|\bTODO:? write\b|\bTBD\b|to be written|\(placeholder\)")


def is_stub(text: str) -> bool:
    """A note with no real body: short, a known placeholder, or only headings."""
    body = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n?", "", text, count=1, flags=re.S)
    prose = "\n".join(l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("#"))
    return len(body.strip()) < STUB_MIN_CHARS or not prose.strip() or bool(_PLACEHOLDER.search(body) and len(prose) < 600)


def remote_path(person: str, day: str, rel: Path) -> str:
    stem = str(rel.with_suffix("")).replace("/", "__")
    return f"inbox/{person}/{day}/{stem}.md"


def identity() -> tuple[str, str]:
    """(person, author email)."""
    email = provenance.author_email()
    person = (getpass.getuser() or email.split("@")[0]).lower()
    return person, email


class _Deadline(Exception):
    pass


def _alarm(signum, frame):
    raise _Deadline("request exceeded its wall-clock deadline")


class Client:
    def __init__(self, cfg: dict):
        self.base = cfg["baseUrl"].rstrip("/")
        self.key = cfg["apiKey"]
        self.org = cfg["organizationId"]

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        query = urllib.parse.urlencode({"organization_id": self.org, **({"path": path} if method == "GET" else {})})
        req = urllib.request.Request(
            f"{self.base}/api/v1/vault/document?{query}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.key}", "User-Agent": USER_AGENT,
                     "Content-Type": "application/json", "Accept": "application/json"})
        # A socket timeout bounds inactivity, not elapsed time; SIGALRM is the real deadline.
        old = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, REQUEST_DEADLINE)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def put(self, path: str, content: str, metadata: dict) -> tuple[int, dict]:
        status, got = self._call("GET", path)
        if status == 404:
            revision = 0
        elif status == 200:
            revision = (got.get("document") or {}).get("revision", 0)
        else:
            return status, got
        return self._call("PUT", path, {"path": path, "content": content,
                                        "parent_revision": revision, "metadata": metadata})


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_FILE)


def _describe(status: int, body: dict) -> str:
    detail = body.get("error") or body
    return f"HTTP {status}: {detail}" if status else f"no response: {detail}"


def _put(client, path: str, content: str, metadata: dict) -> tuple[int, dict]:
    try:
        return client.put(path, content, metadata)
    except Exception as e:  # network error or deadline
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _put_once_more_if_transient(client, path: str, content: str, metadata: dict) -> tuple[int, dict]:
    """PUT, retrying once when there was no response, a 5xx or a 409.

    A lost response is the common case: the server wrote the note but the
    reply never arrived (2026-09-29: the document landed, the run counted an
    error and exited 1). The retry re-reads the revision, and the server
    treats a PUT of identical content as a no-op, so it cannot duplicate.
    """
    status, body = _put(client, path, content, metadata)
    if status == 0 or status >= 500 or status == 409:
        print(f"retrying {path} after {_describe(status, body)}", file=sys.stderr)
        time.sleep(RETRY_DELAY)
        status, body = _put(client, path, content, metadata)
    return status, body


def run(dry_run: bool = False, vault: Optional[Path] = None, client=None, limit: int = 0) -> dict:
    vault = Path(vault or config.VAULT_ROOT)
    person, email = identity()
    day = date.today().isoformat()
    laptop = socket.gethostname().split(".")[0]
    state = _load_state()
    counts = {"pushed": 0, "unchanged": 0, "redacted": 0, "skipped": 0, "skipped_stub": 0, "errors": 0, "not_eligible": 0}
    if client is None and not dry_run:
        client = Client(json.loads(FORESYN_CONFIG.read_text()))
    started = time.monotonic()
    stop_reason = None

    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            note_type = eligible(rel, text)
        except (OSError, UnicodeDecodeError):
            counts["skipped"] += 1
            continue
        if not note_type:
            counts["not_eligible"] += 1
            continue
        if is_stub(text):
            counts["skipped_stub"] += 1
            continue
        try:
            content, n = redact(text)
        except Exception:
            counts["skipped"] += 1  # never upload a note the scanner did not finish
            continue
        sha = hashlib.sha256(content.encode()).hexdigest()
        if state.get(str(rel)) == sha:
            counts["unchanged"] += 1
            continue
        if n:
            counts["redacted"] += 1
        if dry_run:
            counts["pushed"] += 1
            continue
        if time.monotonic() - started > RUN_DEADLINE:
            stop_reason = "run deadline reached; the rest goes next run"
            break
        if limit and counts["pushed"] >= limit:
            stop_reason = f"--limit {limit} reached"
            break
        fm = frontmatter(text)
        assistant = fm.get("assistant", "").lower()
        metadata = {"source": "claude-note", "author": email or fm.get("author", ""),
                    "assistant": assistant if assistant in provenance.ASSISTANTS else "",
                    "laptop": laptop, "source_path": str(rel),
                    "content_sha256": sha, "note_type": note_type, "redactions": n}
        status, body = _put_once_more_if_transient(client, remote_path(person, day, rel), content, metadata)
        if status == 200:
            counts["pushed"] += 1
            state[str(rel)] = sha
            _save_state(state)
        else:
            counts["errors"] += 1
            print(f"error: {rel}: {_describe(status, body)}", file=sys.stderr)
            if status in DETERMINISTIC:  # do not hammer
                stop_reason = _describe(status, body)
                break

    if stop_reason:
        counts["stopped"] = stop_reason
    # Touched on every clean run, including one with nothing to push: the
    # owner's app alerts when this file is older than 26 h.
    if not dry_run and counts["errors"] == 0 and not stop_reason:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "counts": counts}))
    return counts
