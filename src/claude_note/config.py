"""Configuration for claude-note.

Configuration is loaded from (in order of precedence):
1. Environment variables (CLAUDE_NOTE_*)
2. Config file (~/.config/claude-note/config.toml)
3. Built-in defaults

Required: vault_root must be set via env var or config file.
"""

import os
import sys
from pathlib import Path
from typing import Any, Optional

# =============================================================================
# Config Loading
# =============================================================================

_config_cache: Optional[dict] = None


def _get_config_path() -> Path:
    """Get path to config file (XDG standard)."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(xdg_config) / "claude-note" / "config.toml"


def _load_toml_config() -> dict:
    """Load configuration from TOML file."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config_path = _get_config_path()
    if not config_path.exists():
        _config_cache = {}
        return _config_cache

    # Python 3.11+ has tomllib built-in
    if sys.version_info >= (3, 11):
        import tomllib
        with open(config_path, "rb") as f:
            _config_cache = tomllib.load(f)
    else:
        # Fallback for older Python - basic TOML parsing
        _config_cache = _parse_simple_toml(config_path)

    return _config_cache


def _parse_simple_toml(path: Path) -> dict:
    """Simple TOML parser for basic key=value and [section] syntax."""
    result: dict = {}
    current_section: Optional[dict] = None

    with open(path, "r") as f:
        for line in f:
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue

            # Section header
            if line.startswith("[") and line.endswith("]"):
                section_name = line[1:-1].strip()
                result[section_name] = {}
                current_section = result[section_name]
                continue

            # Key-value pair
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()

                # Parse value
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                elif value.lower() == "true":
                    value = True
                elif value.lower() == "false":
                    value = False
                elif value.isdigit():
                    value = int(value)
                elif _is_float(value):
                    value = float(value)

                if current_section is not None:
                    current_section[key] = value
                else:
                    result[key] = value

    return result


def _is_float(s: str) -> bool:
    """Check if string is a valid float."""
    try:
        float(s)
        return "." in s
    except ValueError:
        return False


def _get_config_value(key: str, section: Optional[str] = None, default: Any = None) -> Any:
    """Get a config value from env var or config file."""
    # Environment variable takes precedence
    env_key = f"CLAUDE_NOTE_{key.upper()}"
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value

    # Try config file
    config = _load_toml_config()

    if section:
        section_config = config.get(section, {})
        if key in section_config:
            return section_config[key]
    elif key in config:
        return config[key]

    return default


def _require_vault_root() -> Path:
    """Get vault root, raising clear error if not configured."""
    vault_root = _get_config_value("vault_root")

    if vault_root is None:
        config_path = _get_config_path()
        print(
            f"Error: vault_root not configured.\n\n"
            f"Set CLAUDE_NOTE_VAULT environment variable or create {config_path}:\n\n"
            f'  vault_root = "/path/to/your/vault"\n',
            file=sys.stderr
        )
        sys.exit(1)

    return Path(vault_root).expanduser()


# =============================================================================
# Core Paths
# =============================================================================

VAULT_ROOT = _require_vault_root()
CLAUDE_NOTE_DIR = VAULT_ROOT / ".claude-note"
QUEUE_DIR = CLAUDE_NOTE_DIR / "queue"
STATE_DIR = CLAUDE_NOTE_DIR / "state"
LOGS_DIR = CLAUDE_NOTE_DIR / "logs"
TRANSCRIPTS_DIR = CLAUDE_NOTE_DIR / "transcripts"

# Ingestion output directories
LITERATURE_DIR = VAULT_ROOT / "literature"
INTERNAL_DIR = VAULT_ROOT / "internal"

# =============================================================================
# Timing
# =============================================================================

DEBOUNCE_SECONDS = int(_get_config_value("debounce_seconds", default=15))
POLL_INTERVAL = int(_get_config_value("poll_interval", default=2))
LOCK_TIMEOUT = int(_get_config_value("lock_timeout", default=30))

# =============================================================================
# Open Questions
# =============================================================================

_open_questions_file = _get_config_value("open_questions_file", default="open-questions.md")
OPEN_QUESTIONS_FILE = VAULT_ROOT / _open_questions_file

# Patterns that indicate a question or open item
QUESTION_PATTERNS = [
    "?",
    "unclear",
    "TODO",
    "investigate",
    "open question",
    "need to understand",
    "not sure",
    "figure out",
]

# =============================================================================
# Recursion Prevention
# =============================================================================

RECURSION_MARKERS = [
    ".claude-note/",
    "extracting durable knowledge",
    "claude-note worker",
    "claude-note status",
    "claude-note drain",
    "localhost:8000/events",
]

# =============================================================================
# Synthesis Configuration
# =============================================================================

# Synthesis mode:
#   "log"   - Session logging only, no synthesis
#   "inbox" - Synthesize and append to inbox (safe mode)
#   "route" - Full synthesis with note creation/updates (default)
SYNTH_MODE = _get_config_value("mode", section="synthesis", default="route")

# Inbox file for synthesized knowledge
INBOX_PATH = VAULT_ROOT / _get_config_value("inbox_file", default="claude-note-inbox.md")

# Vault index for linking context
INDEX_PATH = STATE_DIR / "vault_index.json"
INDEX_REFRESH_INTERVAL = int(_get_config_value("index_refresh_interval", default=300))

# Synthesis model
SYNTH_MODEL = _get_config_value("model", section="synthesis", default="claude-sonnet-4-5-20250929")
SYNTH_MAX_TOKENS = int(_get_config_value("max_tokens", section="synthesis", default=4096))
SYNTH_TIMEOUT = int(_get_config_value("timeout", section="synthesis", default=120))

# OpenAI-compatible API for synthesis (e.g. CLIProxyAPI)
# When set, uses HTTP API instead of Claude CLI subprocess
SYNTH_API_BASE = _get_config_value("api_base", section="synthesis", default=None)
SYNTH_API_KEY = _get_config_value("api_key", section="synthesis", default=None)

# =============================================================================
# Cleanup Configuration
# =============================================================================

TIMELINE_MAX_ENTRIES = int(_get_config_value("timeline_max_entries", default=100))
INBOX_DEDUP_ENABLED = _get_config_value("inbox_dedup_enabled", default=True)
INBOX_DEDUP_THRESHOLD = float(_get_config_value("inbox_dedup_threshold", default=0.7))
INBOX_DEDUP_LOOKBACK = int(_get_config_value("inbox_dedup_lookback", default=50))

# =============================================================================
# QMD Semantic Search Configuration
# =============================================================================

_qmd_enabled = _get_config_value("enabled", section="qmd", default=True)
if isinstance(_qmd_enabled, str):
    _qmd_enabled = _qmd_enabled.lower() == "true"

QMD_API_BASE = os.environ.get("CLAUDE_NOTE_QMD_API_BASE") or \
    _get_config_value("api_base", section="qmd", default=None)

QMD_SYNTH_ENABLED = _qmd_enabled
QMD_SYNTH_MAX_NOTES = int(_get_config_value("synth_max_notes", section="qmd", default=5))
QMD_MIN_SCORE = float(_get_config_value("min_score", section="qmd", default=0.3))
QMD_LINK_ENHANCE_ENABLED = _get_config_value("link_enhance_enabled", section="qmd", default=True)
QMD_INGEST_DEDUP_ENABLED = _get_config_value("ingest_dedup_enabled", section="qmd", default=True)
QMD_INGEST_DEDUP_THRESHOLD = float(_get_config_value("ingest_dedup_threshold", section="qmd", default=0.75))

# Merge mode: when a similar concept is found, merge sources instead of skipping
INGEST_MERGE_ENABLED = _get_config_value("ingest_merge_enabled", default=True)
INGEST_MAX_SOURCES_PER_CONCEPT = int(_get_config_value("max_sources_per_concept", default=5))


# =============================================================================
# Helper Functions
# =============================================================================

def ensure_dirs():
    """Ensure all required directories exist."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)


def get_config_summary() -> dict:
    """Return a summary of current configuration for debugging."""
    return {
        "vault_root": str(VAULT_ROOT),
        "config_file": str(_get_config_path()),
        "synth_mode": SYNTH_MODE,
        "synth_model": SYNTH_MODEL,
        "open_questions_file": str(OPEN_QUESTIONS_FILE),
        "qmd_enabled": QMD_SYNTH_ENABLED,
    }
