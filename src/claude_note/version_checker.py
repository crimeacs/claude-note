"""Version checking and update notifications."""

import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from . import config

REPO_URL = "https://github.com/artemiin/claude-note"
RELEASES_API = "https://api.github.com/repos/artemiin/claude-note/releases/latest"
CHECK_INTERVAL_HOURS = 24
VERSION_CHECK_FILE = config.STATE_DIR / "version-check.json"


def get_latest_version() -> str | None:
    """Fetch latest version from GitHub releases API."""
    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "claude-note"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            tag = data.get("tag_name", "")
            return tag.lstrip("v")  # "v1.0.1" -> "1.0.1"
    except Exception:
        return None


def compare_versions(current: str, latest: str) -> int:
    """Compare semver versions. Returns: -1 if current < latest, 0 if equal, 1 if current > latest."""
    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split(".")[:3])
    try:
        return (parse(current) > parse(latest)) - (parse(current) < parse(latest))
    except ValueError:
        return 0


def should_check() -> bool:
    """Return True if enough time has passed since last check."""
    if not VERSION_CHECK_FILE.exists():
        return True
    try:
        data = json.loads(VERSION_CHECK_FILE.read_text())
        last_check = datetime.fromisoformat(data.get("last_check", ""))
        return datetime.now(timezone.utc) - last_check > timedelta(hours=CHECK_INTERVAL_HOURS)
    except Exception:
        return True


def save_check_result(latest: str | None, update_available: bool):
    """Save check result to avoid repeated API calls."""
    VERSION_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_CHECK_FILE.write_text(json.dumps({
        "last_check": datetime.now(timezone.utc).isoformat(),
        "latest_version": latest,
        "update_available": update_available,
    }))


def check_for_update(logger) -> bool:
    """Check for updates and log if available. Returns True if update available."""
    if not should_check():
        # Use cached result
        try:
            data = json.loads(VERSION_CHECK_FILE.read_text())
            if data.get("update_available"):
                logger.info(f"Update available: {__version__} -> {data.get('latest_version')} (run: claude-note update)")
            return data.get("update_available", False)
        except Exception:
            return False

    latest = get_latest_version()
    if latest is None:
        save_check_result(None, False)
        return False

    update_available = compare_versions(__version__, latest) < 0
    save_check_result(latest, update_available)

    if update_available:
        logger.info(f"Update available: {__version__} -> {latest} (run: claude-note update)")

    return update_available


def get_update_status() -> dict:
    """Get current update status for CLI display."""
    # Force fresh check for explicit status query
    latest = get_latest_version()
    if latest is None:
        return {"current": __version__, "latest": None, "update_available": False}

    update_available = compare_versions(__version__, latest) < 0
    return {
        "current": __version__,
        "latest": latest,
        "update_available": update_available,
    }
