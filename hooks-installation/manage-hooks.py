#!/usr/bin/env python3
"""Install or remove claude-note API hooks from Claude Code settings.json."""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOKS_DIR = Path(__file__).parent
API_HOOKS_PATH = HOOKS_DIR / "api-hooks.json"
HOOK_SCRIPT_PATH = HOOKS_DIR / "claude-note-hook.sh"

# Marker to identify our hooks
MARKER = "claude-note-hook.sh"


def load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def backup(settings_path: Path) -> Path:
    """Copy settings.json to hooks-installation/ with timestamp."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = HOOKS_DIR / f"settings.json.backup.{ts}"
    shutil.copy2(settings_path, backup_path)
    return backup_path


def install() -> None:
    if not SETTINGS_PATH.exists():
        print(f"Error: {SETTINGS_PATH} not found", file=sys.stderr)
        sys.exit(1)

    settings = load_json(SETTINGS_PATH)
    api_hooks = load_json(API_HOOKS_PATH)
    hooks = settings.setdefault("hooks", {})

    # Check if already installed
    for event_name in api_hooks:
        for group in hooks.get(event_name, []):
            for hook in group.get("hooks", []):
                if MARKER in hook.get("command", ""):
                    print("claude-note API hooks already installed. Nothing to do.")
                    return

    # Backup before modifying
    backup_path = backup(SETTINGS_PATH)
    print(f"Backup saved: {backup_path}")

    # Resolve absolute path to hook script
    script_path = str(HOOK_SCRIPT_PATH.resolve())

    # Merge: for each event, append our hook entry to the first matcher group
    for event_name, api_groups in api_hooks.items():
        # Replace placeholder with real script path
        our_hook = dict(api_groups[0]["hooks"][0])
        our_hook["command"] = our_hook["command"].replace("<HOOK_SCRIPT_PATH>", script_path)

        if event_name not in hooks:
            hooks[event_name] = [{"hooks": [our_hook]}]
        else:
            hooks[event_name][0]["hooks"].append(our_hook)

    save_json(SETTINGS_PATH, settings)
    print("Installed hooks for: " + ", ".join(api_hooks.keys()))


def remove() -> None:
    if not SETTINGS_PATH.exists():
        print(f"Error: {SETTINGS_PATH} not found", file=sys.stderr)
        sys.exit(1)

    settings = load_json(SETTINGS_PATH)
    hooks = settings.get("hooks", {})

    found = False
    events_cleaned = []

    for event_name in list(hooks.keys()):
        groups = hooks[event_name]
        for group in groups:
            original_len = len(group.get("hooks", []))
            group["hooks"] = [
                h for h in group.get("hooks", [])
                if MARKER not in h.get("command", "")
            ]
            if len(group["hooks"]) < original_len:
                found = True
                events_cleaned.append(event_name)

        # Remove empty groups
        hooks[event_name] = [g for g in groups if g.get("hooks")]
        # Remove empty events
        if not hooks[event_name]:
            del hooks[event_name]

    if not found:
        print("No claude-note API hooks found. Nothing to remove.")
        return

    # Backup before modifying
    backup_path = backup(SETTINGS_PATH)
    print(f"Backup saved: {backup_path}")

    save_json(SETTINGS_PATH, settings)
    print("Removed hooks from: " + ", ".join(set(events_cleaned)))


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("install", "remove"):
        print(f"Usage: {sys.argv[0]} install|remove")
        sys.exit(1)

    if sys.argv[1] == "install":
        install()
    else:
        remove()
