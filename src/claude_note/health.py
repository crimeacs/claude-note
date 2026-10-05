"""
`claude-note status --json`: one machine-readable health report.

Built for another program (the Sweat app's setup checklist) to call on a
timer, so it is cheap and never fails: no network, every probe bounded, and it
works on a machine where claude-note is not configured yet (it must not import
`config`, which exits when vault_root is unset).

Exit code is 0 when `ok` is true, 1 otherwise; the JSON is printed either way.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, provenance

HOME = Path.home()
LOGS = HOME / "Library/Logs/claude-note"
STATE_DIR = HOME / ".local/share/claude-note"
LAUNCH_AGENTS = HOME / "Library/LaunchAgents"
WORKER_LABEL = "com.claude-note.worker"
PUSH_LABEL = "com.claude-note.push"
PUSH_STALE_HOURS = 26          # the push runs daily at 03:00
IMPORT_STALE_SECONDS = 2 * 1800 + 600


def _config() -> tuple[Path, dict]:
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = Path(xdg) / "claude-note" / "config.toml"
    return path, provenance._config_toml()


def _launchd_loaded(label: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _heartbeat(path: Path, stale_after: float) -> dict:
    if not path.exists():
        return {"last_ok": None, "age_seconds": None, "stale": True, "counts": {}}
    age = time.time() - path.stat().st_mtime
    try:
        counts = json.loads(path.read_text()).get("counts", {})
    except (OSError, ValueError, AttributeError):
        counts = {}
    return {"last_ok": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(path.stat().st_mtime)),
            "age_seconds": int(age), "stale": age > stale_after, "counts": counts}


def _hooks_installed(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    groups = (data.get("hooks") or {}).values() if isinstance(data, dict) else []
    return any("claude-note enqueue" in str(h.get("command", ""))
               for event in groups if isinstance(event, list)
               for g in event if isinstance(g, dict)
               for h in g.get("hooks", []) if isinstance(h, dict))


def _which(name: str) -> str:
    # launchd and GUI apps get a minimal PATH; look where the installers put things.
    extra = [HOME / ".local/bin", HOME / ".claude/local", HOME / ".bun/bin", HOME / ".npm-global/bin",
             Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    nvm = HOME / ".nvm/versions/node"
    if nvm.is_dir():  # newest Node first
        extra += sorted((d / "bin" for d in nvm.iterdir()), key=lambda d: [
            int(x) if x.isdigit() else 0 for x in d.parent.name.lstrip("v").split(".")], reverse=True)
    found = shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", "")] + [str(p) for p in extra]))
    return found or ""


NOTE_SCAN_DEADLINE = 5.0        # seconds; a huge vault reports a partial count
PUSH_STATE = STATE_DIR / "push-state.json"


def _notes(vault: Path) -> dict:
    """What the loop produced: session logs, knowledge notes, how many of those the
    push will share, and how many it has sent. One frontmatter read per note."""
    out = {"sessions": 0, "knowledge": 0, "shareable": 0, "pushed": 0, "complete": True}
    try:
        out["pushed"] = len(json.loads(PUSH_STATE.read_text()))
    except (OSError, ValueError, TypeError):
        pass
    try:
        from . import push  # imports config, which needs vault_root: only called when set
    except (Exception, SystemExit):
        out["complete"] = False
        return out
    deadline = time.monotonic() + NOTE_SCAN_DEADLINE
    for path in vault.rglob("*.md"):
        if time.monotonic() > deadline:
            out["complete"] = False
            break
        rel = path.relative_to(vault)
        if any(part.startswith(".") for part in rel.parts[:-1]) or path.is_symlink():
            continue
        if rel.name.startswith("claude-session-"):
            out["sessions"] += 1
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue
        out["knowledge"] += 1
        if push.eligible(rel, head):
            out["shareable"] += 1
    return out


def _synthesis(vault: Path) -> dict:
    """Failures in the newest worker log: a synthesis that cannot run (no claude
    CLI on the worker's PATH, an unknown model) leaves session logs and no notes."""
    logs = sorted((vault / ".claude-note/logs").glob("worker-[0-9]*.log"))
    out = {"failed": 0, "succeeded": 0, "last_error": None, "log": str(logs[-1]) if logs else None}
    if not logs:
        return out
    try:
        with open(logs[-1], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return out
    for line in tail:
        if "Synthesis failed" in line:
            out["failed"] += 1
            out["last_error"] = line.split("Synthesis failed", 1)[1].lstrip(" :")[:300]
        elif "Updated session summary" in line or "Created notes:" in line:
            out["succeeded"] += 1
    return out


def report() -> dict:
    config_path, cfg = _config()
    vault_root = os.environ.get("CLAUDE_NOTE_VAULT_ROOT") or cfg.get("vault_root") or ""
    vault = Path(vault_root).expanduser() if vault_root else None
    try:
        installed_from = json.loads((STATE_DIR / "installed-from.json").read_text())
    except (OSError, ValueError):
        installed_from = None

    worker_plist = LAUNCH_AGENTS / f"{WORKER_LABEL}.plist"
    push_plist = LAUNCH_AGENTS / f"{PUSH_LABEL}.plist"
    push = {"agent_installed": push_plist.exists(), "loaded": _launchd_loaded(PUSH_LABEL),
            **_heartbeat(LOGS / "push.ok", PUSH_STALE_HOURS * 3600)}
    sweep = _heartbeat(LOGS / "import-sweep.ok", IMPORT_STALE_SECONDS)
    sweep_counts = sweep.pop("counts")
    sweep["problems"] = list(sweep_counts.get("problems", [])) if isinstance(sweep_counts, dict) else []
    queue_dir = vault / ".claude-note/queue" if vault else None

    out = {
        "version": __version__,
        "configured": bool(vault_root),
        "config_file": str(config_path),
        "vault_root": str(vault) if vault else None,
        "vault_exists": bool(vault and vault.is_dir()),
        "author": provenance.author_email(),
        "synth_mode": (cfg.get("synthesis") or {}).get("mode", "route") if vault_root else None,
        "installed_from": installed_from,
        "binaries": {name: _which(name) for name in ("claude-note", "uv", "claude", "qmd", "foresyn")},
        "worker": {"agent_installed": worker_plist.exists(), "loaded": _launchd_loaded(WORKER_LABEL)},
        "push": push,
        "import_sweep": sweep,
        "hooks": {"claude_code": _hooks_installed(HOME / ".claude/settings.json"),
                  "codex": _hooks_installed(HOME / ".codex/hooks.json")},
        "queue_files": len(list(queue_dir.glob("*.jsonl"))) if queue_dir and queue_dir.is_dir() else 0,
        "notes": _notes(vault) if vault and vault.is_dir() else None,
        "synthesis": _synthesis(vault) if vault and vault.is_dir() else None,
    }

    problems = []
    if not out["configured"]:
        problems.append(f"not configured: no vault_root in {config_path}")
    elif not out["vault_exists"]:
        problems.append(f"vault folder missing: {vault}")
    if not out["author"]:
        problems.append("no author email (set `author` in config.toml)")
    if sys.platform == "darwin":
        if not out["worker"]["loaded"]:
            problems.append("worker not running (launchd com.claude-note.worker)")
        if not push["agent_installed"]:
            problems.append("daily push not installed (claude-note push --install-agent)")
        elif push["last_ok"] is None:
            pass  # installed but has not reached 03:00 yet: not a problem
        elif push["stale"]:
            problems.append(f"last clean push {push['age_seconds'] // 3600} h ago; see {LOGS}/push.log")
    if not out["hooks"]["claude_code"]:
        problems.append("Claude Code hooks missing (claude-note install-claude-hooks)")
    if not out["binaries"]["claude"] and out["synth_mode"] not in (None, "log"):
        problems.append("claude CLI not found: sessions are logged but not synthesized")
    if out["binaries"]["claude"] and out["synth_mode"] == "log":
        problems.append(f"synthesis is off (mode = \"log\" in {config_path}): sessions are logged "
                        "but never become notes; set mode = \"route\"")
    synth = out["synthesis"] or {}
    if synth.get("failed") and not synth.get("succeeded"):
        problems.append(f"synthesis failing: {synth.get('last_error')}")
    problems.extend(f"import: {p}" for p in sweep["problems"])
    out["problems"] = problems
    out["ok"] = not problems
    return out


def main() -> int:
    out = report()
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["ok"] else 1
