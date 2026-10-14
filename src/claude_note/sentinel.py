"""Known-fix sentinel: when a command fails, recall what fixed it last time.

Runs as a Claude Code PostToolUse / PostToolUseFailure hook on Bash. It reads the
command's OUTPUT (never the command text: a `cat` of a file that merely contains
an error string must not trigger it), picks the most specific error line, and
does two things:

1. Classifies observed provider codes and output conservatively, distinguishing
   persistent budget/auth blockers from transient failures and ordinary exit codes.
2. Searches the knowledge index for that error, preferring notes typed `gotcha`,
   `pattern` or `decision`, so a fix someone already found is in front of the
   agent before its second attempt.

Repeat advisories have a short TTL and state-generation invalidation. Fails open.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
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
    {"match": r"\binvalid_grant\b|\binvalid_rapt\b|token (?:has )?expired",
     "advice": "Verify the provider's invalid or expired grant and credential source before re-authenticating. Do not delete credentials based on a log phrase alone."},
    {"match": r"insufficient ?permission|insufficientPermissions|insufficient.?scope",
     "advice": "Verify the required OAuth scope and account; obtain the missing grant through the approved flow."},
    {"match": r"\b401\b|unauthori[sz]ed|authenticationfailed|invalid api key|invalid_api_key",
     "advice": "Authentication failed. Check which credential the call actually used and whether it "
               "expired before regenerating anything; never retry the identical call."},
    {"match": r"credit balance|insufficient_quota|quota.?exceeded|spend limit",
     "advice": "The provider reports exhausted quota or credits. Check limits and funding before resuming."},
    {"match": r"rate.?limit|\b429\b",
     "advice": "Honor Retry-After/backoff and verify idempotency before a bounded retry."},
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
    provider = resp.get("error")
    if isinstance(provider, dict) and (provider.get("code") or provider.get("message")):
        return json.dumps(provider, sort_keys=True)
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


def first_time(session: str, signature: str, *, generation: str = "", ttl: float = 300) -> bool:
    """Reserve before retrieval; state changes or TTL permit reconsideration."""
    path = _seen_path()
    now = time.time()
    key = hashlib.sha256((generation + "\n" + signature).encode()).hexdigest()[:24]
    try:
        import fcntl
        from . import state_io
        descriptor = state_io.open_lock(path.with_suffix(".lock"))
        try:
            deadline = time.monotonic() + .05
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return True
                    time.sleep(.005)
            try:
                data = state_io.read_json(path)
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            data = {k: v for k, v in data.items() if isinstance(v, dict)
                    and isinstance(v.get("t"), (int, float)) and now - v["t"] < 7 * 86400}
            data = dict(sorted(data.items(), key=lambda item: item[1]["t"])[-1000:])
            entry = data.setdefault(session or "-", {"t": now, "seen": {}})
            seen = entry.get("seen", {})
            if not isinstance(seen, dict):
                seen = {}
            seen = {k: t for k, t in seen.items() if isinstance(t, (int, float)) and now - t < ttl}
            if key in seen:
                return False
            seen[key] = now
            entry.update(t=now, seen=dict(sorted(seen.items(), key=lambda item: item[1])[-50:]))
            state_io.atomic_json(path, data)
        finally:
            os.close(descriptor)
    except (OSError, ValueError, ImportError):
        return True  # Failure to persist must not suppress the advisory hook.
    return True


def classify_failure(payload: dict, text: str) -> dict:
    """Conservative advisory taxonomy; no classification executes remediation."""
    response = payload.get("tool_response")
    response = response if isinstance(response, dict) else {}
    provider_error = response.get("error")
    provider_error = provider_error if isinstance(provider_error, dict) else {}
    code = str(provider_error.get("code") or response.get("code") or "").lower()
    status = response.get("status_code", response.get("http_status"))
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if status is None:
        http = re.search(r"(?:\bHTTP(?:/\d(?:\.\d)?)?\s+|(?:status|error)\s*[:=]\s*)(40[13]|429|50[234])\b", text, re.I)
        status = int(http.group(1)) if http else None
    command = str((payload.get("tool_input") or {}).get("command", ""))
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    exit_code = response.get("exit_code", response.get("exitCode", response.get("returnCode")))
    if (exit_code == 1 and not response.get("stderr") and argv
            and Path(argv[0]).name in {"grep", "rg", "diff", "cmp", "test", "["}
            and not re.search(r"[;|]|&&", command)):
        return {"class": "expected_nonzero", "code": "", "retry_after": None, "advisory_only": True,
                "advice": "This tool uses exit 1 for a negative comparison or no match; inspect its result rather than treating it as an infrastructure failure."}
    active = re.sub(r"\b(?:not|without|no)\s+(?:an?\s+)?(?:invalid_grant|invalid_rapt|authentication error|token expired)\b",
                    "", text, flags=re.I).lower()
    evidence = code + "\n" + active
    headers = response.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    retry = response.get("retry_after") or headers.get("Retry-After")
    if retry is None:
        match = re.search(r"retry-after\s*[:=]\s*(\d+)", active)
        retry = int(match.group(1)) if match else None
    kind, advice = "unknown", "Inspect the failure and current state before choosing recovery; a nonzero exit alone does not establish the cause."
    if re.search(r"insufficient_quota|credit balance|billing[_ ]hard[_ ]limit|quota (?:exceeded|exhausted)|hit your (?:weekly|daily|monthly) limit|usage limit.{0,80}resets", evidence):
        kind, advice = "budget", "The provider reports exhausted quota or credits. Check limits and funding; resume after the blocking condition changes."
    elif re.search(r"\benospc\b|no space left on device|disk (?:is )?full", evidence):
        kind, advice = "resource", "The disk is full. Inspect usage and choose disposable files to remove; wait for capacity before repeating writes."
    elif re.search(r"\binvalid_grant\b|\binvalid_rapt\b|token (?:has )?expired|invalid_api_key|invalid api key|not logged in|authentication required|invalid authentication credentials", evidence) or status == 401 or re.search(r"\b401\b.*(?:unauthori[sz]ed|error)|unauthori[sz]ed.*\b401\b", active):
        kind, advice = "authentication", "The provider reports an invalid or expired credential. Verify the account and credential source; re-authenticate if confirmed. Do not delete cached credentials based on log text alone."
    elif status == 403 or re.search(r"insufficientpermissions|insufficient[_ ]scopes?|permission[_ ]denied|\b403\b|\btcc\b|screen recording permission", evidence):
        kind, advice = "authorization", "Check the required permission or OAuth scope and intended account. Obtain the missing grant through the approved flow before retrying."
    elif status in (429, 502, 503, 504) or re.search(r"rate[_ -]?limit(?:_exceeded)?|\b429\b|connection (?:reset|timed out)|\btimeout\b|timed out|temporarily unavailable", evidence):
        kind, advice = "transient", "This may be transient. Honor Retry-After/backoff and verify operation idempotency before a bounded retry; stop if it persists."
    elif status in (400, 422) or re.search(r"invalid_argument|invalid input|validation error", evidence):
        kind, advice = "invalid_input", "Correct rejected input before retrying; inspect the provider's validation details."
    return {"class": kind, "code": code, "retry_after": retry, "advisory_only": True, "advice": advice}


def _generation(payload: dict, cfg: dict) -> str:
    try:
        config_revision = recall_mod._config_path().stat().st_mtime_ns
    except OSError:
        config_revision = 0
    return hashlib.sha256(json.dumps({"config": config_revision, "cwd": payload.get("cwd", ""),
        "state": payload.get("state_revision", ""), "credentials": payload.get("credential_revision", ""),
        "environment": payload.get("environment_revision", ""), "generation": cfg.get("generation", "")},
        sort_keys=True).encode()).hexdigest()


def analyse(payload: dict, settings: Optional[recall_mod.Settings] = None,
            runner=recall_mod._run_qmd) -> Optional[dict]:
    text = failure_text(payload)
    if not text:
        return None
    line = error_line(text) or text.strip().splitlines()[-1][:300]
    cfg = _settings()
    classification = classify_failure(payload, text[-6000:])
    advice = [classification["advice"]] if recall_mod._as_bool(cfg.get("default_rules"), True) else []
    advice += matching_advice(text[-6000:], [r for r in cfg.get("rules", []) if isinstance(r, dict)])
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
        return {"line": line, "advice": [], "result": result, "hits": [], "classification": classification}
    return {"line": line, "advice": advice, "result": result, "hits": hits, "classification": classification}


def format_context(found: dict) -> str:
    lines = [f"claude-note sentinel: the last command failed with: {found['line']!r}"]
    if found.get("classification"):
        lines.append(f"Failure class: {found['classification']['class']} (advisory; verify before acting).")
    if found["advice"]:
        lines.append("")
        lines += [f"- {a}" for a in found["advice"]]
    if found["hits"]:
        lines += ["", "Notes about this failure (open one before applying it; it may be stale):"]
        for h in found["hits"]:
            lines.append(f"- {h.title}{f' ({h.type})' if h.type else ''}  [{h.path or h.uri}]")
            lines.append(f"    {h.snippet}")
    lines += ["", "Choose recovery from the observed cause and operation's retry safety. When you verify "
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
        text = failure_text(payload)
        if not text:
            return 0
        line = error_line(text) or text.strip().splitlines()[-1][:300]
        classification = classify_failure(payload, text[-6000:])
        if classification["class"] == "expected_nonzero":
            return 0
        sig = hashlib.sha1((classification["class"] + "\n" + re.sub(r"\d+", "#", line)).encode()).hexdigest()[:12]
        cfg = _settings()
        ttl = max(1, min(3600, float(cfg.get("repeat_ttl_seconds", 300))))
        if not first_time(str(payload.get("session_id") or ""), sig, generation=_generation(payload, cfg), ttl=ttl):
            return 0
        found = analyse(payload)
    except Exception:
        return 0
    if not found or (not found["advice"] and not found["hits"]):
        return 0
    if found["result"] and recall_mod.Settings.load().log:
        recall_mod.log_event("failure", found["result"], {"advice": len(found["advice"])})
    event = payload.get("hook_event_name") or "PostToolUse"
    recall_mod.emit(event, format_context(found))
    return 0
