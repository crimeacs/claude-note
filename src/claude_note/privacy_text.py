"""Config-free secret scanning and conservative frontmatter scalar views."""

import re

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


_FRONTMATTER_OPEN = re.compile(r"\A\ufeff?---[ \t]*(?:\r?\n|\Z)")
_FRONTMATTER_BLOCK = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.S | re.M)


def frontmatter(text: str) -> dict:
    """Top-level scalar keys of a YAML frontmatter block (no YAML dependency)."""
    m = _FRONTMATTER_BLOCK.match(text)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        kv = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            out[kv.group(1)] = kv.group(2).strip().strip("\"'")
    return out

