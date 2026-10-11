"""Known-fix sentinel: when a command fails, recall what fixed it last time.

Runs as a Claude Code PostToolUse / PostToolUseFailure hook on Bash. It reads the
command's OUTPUT (never the command text: a `cat` of a file that merely contains
an error string must not trigger it), picks the most specific error line, and
does two things:

1. Matches built-in and configured signatures for failures that are
   deterministic: auth, quota and disk-full errors never fix themselves on retry,
   and agents burn many turns retrying them.
2. Searches the knowledge index for that error, preferring notes typed `gotcha`,
   `pattern` or `decision`, so a fix someone already found is in front of the
   agent before its second attempt.

Each distinct error is reported once per session. Fails open, bounded in time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from . import recall as recall_mod

ERROR_LINE = re.compile(
    r"(error|exception|traceback|fatal|failed|failure|denied|forbidden|unauthori[sz]ed|"
    r"not found|no such file|enospc|no space left|timed? ?out|refused|\b40[13]\b|\b429\b|"
    r"quota|rate.?limit|invalid_grant|credit balance|permission)", re.I)

# Signatures that mean "do not retry the identical call".
DEFAULT_RULES = [
    {"match": r"invalid_grant|invalid_rapt|token (has )?expired|refresh token",
     "advice": "Expired or revoked OAuth grant. Retrying cannot work: clear the cached token and "
               "re-authenticate, then retry once."},
    {"match": r"insufficient ?permission|insufficientPermissions|insufficient.?scope",
     "advice": "Missing OAuth scope. Existing tokens keep their old scopes: delete the stored token "
               "and re-consent with every scope requested in one flow."},
    {"match": r"\b401\b|unauthori[sz]ed|authenticationfailed|invalid api key|invalid_api_key",
     "advice": "Authentication failed. Check which credential the call actually used and whether it "
               "expired before regenerating anything; never retry the identical call."},
    {"match": r"credit balance|insufficient_quota|quota.?exceeded|resource_exhausted|"
              r"rate.?limit|spend limit|\b429\b",
     "advice": "Quota, spend or rate limit. Deterministic until the limit resets or the account is "
               "topped up: stop the batch, switch to a funded backend, or wait."},
    {"match": r"enospc|no space left on device|disk (is )?full",
     "advice": "The disk is full. Nothing that writes will work: free space first (caches, build "
               "outputs, old artifacts), then retry."},
    {"match": r"-3801|declined tccs|not authorized to capture|screen recording",
     "advice": "macOS privacy permission denied. There is no programmatic fix: a person must enable "
               "it in System Settings > Privacy & Security, for the exact binary that runs."},
]

PATH_OR_NOISE = re.compile(r"(/[^\s:'\"]+)|(\b0x[0-9a-f]+\b)|(\b[0-9a-f]{12,}\b)|(\d{4,})", re.I)
# Exception class names and exit-code boilerplate rank as rare proper nouns and
# zero a conjunctive query; the words AFTER them carry the meaning.
ERROR_NOISE = re.compile(r"\b\w*(Error|Exception|Warning)\b:?|\bexit (code|status) \d+|\btraceback\b", re.I)
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
DOTTED = re.compile(r"\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){1,}\b")
# Identifiers that mean one thing on their own: insufficientPermissions, invalid_grant,
# ENOSPC, ERR_SSL_PROTOCOL_ERROR. Searched singly, since keyword search has no OR.
IDENTIFIER = re.compile(r"\b(?:[a-z]+[A-Z][A-Za-z]+|[A-Za-z]+_[A-Za-z_]+|[A-Z][A-Z0-9_]{3,})\b")
PREFER = ("gotcha", "pattern", "decision", "reference", "feedback")


def _settings() -> dict:
    return (recall_mod._load_toml().get("sentinel", {}) or {})


def failure_text(payload: dict) -> str:
    """Only output; never the command. Empty when nothing failed."""
    if payload.get("tool_name") not in (None, "", "Bash"):
        return ""
    err = payload.get("error")
    if isinstance(err, str) and err.strip():
        return err
    resp = payload.get("tool_response")
    if isinstance(resp, str):
        return resp if ERROR_LINE.search(resp) else ""
    if not isinstance(resp, dict):
        return ""
    stderr = str(resp.get("stderr") or "")
    stdout = str(resp.get("stdout") or "")
    code = resp.get("exit_code", resp.get("exitCode", resp.get("returnCode")))
    failed = (isinstance(code, int) and code != 0) or bool(resp.get("is_error"))
    if failed:
        return stderr + "\n" + stdout[-4000:]
    # A zero exit with error-looking stderr still counts (tools that exit 0 on error);
    # error-looking stdout alone does not: that is usually a file being read.
    return stderr if ERROR_LINE.search(stderr) else ""


def error_line(text: str) -> str:
    """The most specific error line: matching, wordy, and late (errors come last)."""
    best, best_score = "", -1
    lines = [l.strip() for l in text.splitlines() if l.strip()][-60:]
    for i, line in enumerate(lines):
        if not ERROR_LINE.search(line):
            continue
        words = len(re.findall(r"[A-Za-z]{3,}", line))
        score = min(words, 12) * 2 + i / max(len(lines), 1)
        if score > best_score:
            best, best_score = line, score
    return best[:300]


def matching_advice(text: str, rules: list[dict]) -> list[str]:
    out = []
    for rule in rules:
        try:
            if re.search(rule.get("match", "(?!)"), text, re.I):
                advice = str(rule.get("advice", "")).strip()
                if advice and advice not in out:
                    out.append(advice)
        except re.error:
            continue
    return out


def _seen_path() -> Path:
    return recall_mod.HOME / ".local" / "share" / "claude-note" / "sentinel-seen.json"


def first_time(session: str, signature: str) -> bool:
    """True the first time this session sees this error signature."""
    path = _seen_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    now = time.time()
    data = {k: v for k, v in data.items() if now - v.get("t", 0) < 7 * 86400}
    entry = data.setdefault(session or "-", {"t": now, "sigs": []})
    if signature in entry["sigs"]:
        return False
    entry["sigs"] = (entry["sigs"] + [signature])[-50:]
    entry["t"] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except OSError:
        pass
    return True


def analyse(payload: dict, settings: Optional[recall_mod.Settings] = None,
            runner=recall_mod._run_qmd) -> Optional[dict]:
    text = failure_text(payload)
    if not text:
        return None
    line = error_line(text) or text.strip().splitlines()[-1][:300]
    cfg = _settings()
    rules = ([] if not recall_mod._as_bool(cfg.get("default_rules"), True) else DEFAULT_RULES)
    rules = rules + [r for r in cfg.get("rules", []) if isinstance(r, dict)]
    advice = matching_advice(text[-6000:], rules)
    stripped = DOTTED.sub(" ", URL.sub(" ", line))
    query_text = ERROR_NOISE.sub(" ", PATH_OR_NOISE.sub(" ", stripped)).lower()
    ids = []
    for tok in IDENTIFIER.findall(ERROR_NOISE.sub(" ", stripped)):
        if tok.lower() not in {t.lower() for t in ids} and tok.upper() not in {"HTTP", "HTTPS", "JSON", "NULL", "NONE", "TRUE", "FALSE"}:
            ids.append(tok)
    settings = settings or recall_mod.Settings.load()
    settings.min_prompt = 8
    settings.inject = int(cfg.get("inject", 2))
    result = recall_mod.recall(query_text, settings, runner, prefer_types=PREFER,
                               extra_rungs=[[t] for t in ids[:3]])
    hits = [h for h in result.hits if h.type in PREFER or h.memory] or result.hits[:1]
    if not advice and not hits:
        return {"line": line, "advice": [], "result": result, "hits": []}
    return {"line": line, "advice": advice, "result": result, "hits": hits}


def format_context(found: dict) -> str:
    lines = [f"claude-note sentinel: the last command failed with: {found['line']!r}"]
    if found["advice"]:
        lines.append("")
        lines += [f"- {a}" for a in found["advice"]]
    if found["hits"]:
        lines += ["", "Notes about this failure (open one before applying it; it may be stale):"]
        for h in found["hits"]:
            lines.append(f"- {h.title}{f' ({h.type})' if h.type else ''}  [{h.path or h.uri}]")
            lines.append(f"    {h.snippet}")
    lines += ["", "Do not retry the identical command until something has changed. When you find "
                  "the fix, record it: `claude-note remember --type gotcha \"<error>: <cause> -> <fix>\"`."]
    return "\n".join(lines)


def hook_main(stdin=None) -> int:
    if os.environ.get("CLAUDE_CODE_HOOKS_ENABLED") == "false":
        return 0
    if not recall_mod._as_bool(_settings().get("enabled"), True):
        return 0
    try:
        payload = json.load(stdin or sys.stdin)
    except Exception:
        return 0
    try:
        found = analyse(payload)
    except Exception:
        return 0
    if not found or (not found["advice"] and not found["hits"]):
        return 0
    sig = hashlib.sha1(re.sub(r"\d+", "#", found["line"]).encode()).hexdigest()[:12]
    if not first_time(str(payload.get("session_id") or ""), sig):
        return 0
    if found["result"] and recall_mod.Settings.load().log:
        recall_mod.log_event("failure", found["result"], {"advice": len(found["advice"])})
    event = payload.get("hook_event_name") or "PostToolUse"
    recall_mod.emit(event, format_context(found))
    return 0
