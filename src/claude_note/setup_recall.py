"""Wire the knowledge loop end to end, and prove it works.

`claude-note setup` turns capture into a loop:

* registers the vault (and, optionally, Claude Code's own per-project memories
  and agent rule files) as QMD collections, so one index spans them;
* installs the recall hook (UserPromptSubmit) and the failure sentinel
  (PostToolUse / PostToolUseFailure on Bash) next to the capture hooks;
* adds a short "recall before action" rule to the agent instruction files, so
  agents also search deliberately when the automatic lookup misses;
* schedules an hourly `claude-note refresh` (text index, then vectors) with a
  lock, hard timeouts and a heartbeat.

`claude-note doctor` separates *configured* from *working*: it plants a canary
note in a throwaway QMD index, runs the real hook commands against it, and
reports what an agent would have seen.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Optional

from . import recall as recall_mod

HOME = Path.home()
PUBLIC_CANARY_COLLECTION = "claude-note-public-canary"
CAPABILITY_RECEIPT = HOME / ".local/share/claude-note/recall-capabilities.json"
SHARE = HOME / ".local" / "share" / "claude-note"
HEARTBEAT = SHARE / "refresh.ok"
REFRESH_LOG = SHARE / "refresh.log"
LOCKDIR = SHARE / "refresh.lock"
PLIST_LABEL = "com.claude-note.qmd-refresh"
SERVER_LABEL = "com.claude-note.recall-server"

RULE_START = "<!-- claude-note:recall-rule:start -->"
RULE_END = "<!-- claude-note:recall-rule:end -->"
RULE_BODY = """## Recall before action (claude-note)

Prior sessions are indexed. Use them.

1. Before work that touches past work, an incident, a recurring error or a decision,
   search first: `claude-note recall "<two or three content words>"`
   (or `qmd vsearch "<question>"` when you do not know the vocabulary).
2. A hit is a lead, not an answer. Open the note (`qmd get <uri>`), check that it
   still applies to the code and data in front of you, then act.
3. A miss is not evidence that nothing is known: keyword search is conjunctive.
   Drop a word and search again before concluding.
4. When you learn something durable, write it back the same session:
   `claude-note remember --type gotcha|pattern|decision "<what, why, the fix>"`.
5. Never retry a failed auth, quota or disk-full command unchanged; those fail
   the same way every time."""


# --------------------------------------------------------------------------- #
# QMD collections
# --------------------------------------------------------------------------- #


def qmd_exe() -> Optional[str]:
    return recall_mod.qmd_executable(recall_mod.Settings.load())


def registered_collections(index: str = "") -> dict[str, Path]:
    return recall_mod._collection_dirs(index)


def add_collection(path: Path, name: str, mask: str, index: str = "", dry_run: bool = False) -> str:
    exe = qmd_exe()
    if not exe:
        return "skipped (qmd not installed)"
    existing = registered_collections(index)
    for n, p in existing.items():
        if p.expanduser().resolve() == path.expanduser().resolve():
            return f"already indexed as '{n}'"
    if name in existing:
        return f"name '{name}' is taken by {existing[name]}; skipped"
    if dry_run:
        return "would add"
    cmd = [exe] + (["--index", index] if index else []) + [
        "collection", "add", str(path), "--name", name, "--mask", mask]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    return "added" if proc.returncode == 0 else f"failed: {(proc.stderr or proc.stdout).strip()[:200]}"


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #


def _write_json(path: Path, data: dict) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _has_command(groups: list, needle: str) -> bool:
    return any(needle in h.get("command", "") for g in groups for h in g.get("hooks", []))


def install_hooks(path: Path, exe: str, *, codex: bool = False, dry_run: bool = False) -> list[str]:
    """Add recall (and, for Claude Code, the sentinel). Idempotent; backs up first."""
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    wanted = [("UserPromptSubmit", None, f"{exe} hook recall", 10,
               "Notes vault: recall prior knowledge for this prompt")]
    if not codex:
        for event in ("PostToolUse", "PostToolUseFailure"):
            wanted.append((event, "Bash", f"{exe} hook sentinel", 10,
                           "Notes vault: look up known fixes for a failed command"))
    added = []
    for event, matcher, command, timeout, status in wanted:
        groups = hooks.setdefault(event, [])
        needle = command.split(" ", 1)[1]  # "hook recall" / "hook sentinel"
        if _has_command(groups, needle):
            continue
        entry: dict = {"type": "command", "command": command, "timeout": timeout}
        if codex:
            entry["statusMessage"] = status
        group: dict = {"hooks": [entry]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        added.append(f"{event}{f' [{matcher}]' if matcher else ''}: {needle}")
    if added and not dry_run:
        _write_json(path, data)
    return added


def hooks_status(path: Path) -> dict[str, bool]:
    try:
        hooks = json.loads(path.read_text()).get("hooks", {})
    except (OSError, ValueError):
        return {"recall": False, "sentinel": False, "capture": False}
    flat = " ".join(h.get("command", "") for groups in hooks.values() for g in groups
                    for h in g.get("hooks", []))
    return {"recall": "hook recall" in flat, "sentinel": "hook sentinel" in flat,
            "capture": "claude-note enqueue" in flat}


# --------------------------------------------------------------------------- #
# Agent instruction files
# --------------------------------------------------------------------------- #


def install_rule(path: Path, dry_run: bool = False) -> str:
    text = path.read_text() if path.exists() else ""
    block = f"{RULE_START}\n{RULE_BODY}\n{RULE_END}"
    if RULE_START in text and RULE_END in text:
        new = re.sub(re.escape(RULE_START) + r".*?" + re.escape(RULE_END), block, text, flags=re.S)
        if new == text:
            return "up to date"
        verdict = "updated"
    else:
        new = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
        verdict = "added"
    if not dry_run:
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
    return verdict if not dry_run else f"would be {verdict}"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def ensure_recall_config(memory_collections: list[str], dry_run: bool = False) -> str:
    path = recall_mod._config_path()
    text = path.read_text() if path.exists() else ""
    if re.search(r"^\[recall\]", text, re.M):
        return "has a [recall] section (left as is)"
    lines = ["", "[recall]", "enabled = true",
             "collections = []          # empty = every QMD collection",
             f"memory_collections = {json.dumps(memory_collections)}",
             "inject = 4", "timeout = 6", "", "[sentinel]", "enabled = true", ""]
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write("\n".join(lines))
    return "added [recall] and [sentinel]" if not dry_run else "would add [recall] and [sentinel]"


# --------------------------------------------------------------------------- #
# Hourly refresh
# --------------------------------------------------------------------------- #


def _log(line: str) -> None:
    SHARE.mkdir(parents=True, exist_ok=True)
    with open(REFRESH_LOG, "a") as fh:
        fh.write(f"{datetime.now():%Y-%m-%dT%H:%M:%S} {line}\n")


def _freshness_config(index: str) -> tuple[bytes, str]:
    """Read the bounded collection configuration used by this index."""
    name = f"{index or 'index'}.yml"
    directory = Path(os.environ["QMD_CONFIG_DIR"]) if os.environ.get("QMD_CONFIG_DIR") else (
        Path(os.environ.get("XDG_CONFIG_HOME", str(recall_mod.HOME / ".config"))) / "qmd")
    path = directory / name
    try:
        with path.open("rb") as handle:
            body = handle.read(256_001)
        if len(body) <= 256_000:
            identity = hashlib.sha256(str(path.absolute()).encode() + b"\0" + body).hexdigest()
            return body, identity
    except OSError:
        pass
    return b"", ""


def _freshness_collections(index: str) -> tuple[dict[str, Path], str]:
    """Return only full-root Markdown collections without custom exclusions."""
    body, identity = _freshness_config(index)
    if not identity:
        return {}, ""
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return {}, ""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    patterns, ignored = {}, set()
    if isinstance(data, dict) and isinstance(data.get("collections"), dict):
        patterns = {key: value.get("pattern") for key, value in data["collections"].items()
                    if isinstance(value, dict)}
        ignored = {key for key, value in data["collections"].items()
                   if isinstance(value, dict) and value.get("ignore")}
    else:
        current = None
        in_collections = False
        for line in text.splitlines():
            if line == "collections:":
                in_collections = True
                continue
            if line and not line.startswith(" ") and not line.startswith("#"):
                in_collections = False
                current = None
            if not in_collections:
                continue
            collection = re.fullmatch(r"  ([A-Za-z0-9_.-]+):\s*", line)
            if collection:
                current = collection[1]
            elif line and not line.startswith(" "):
                current = None
            elif current:
                pattern = re.fullmatch(r"    pattern:\s*(.*?)\s*", line)
                if pattern:
                    patterns[current] = pattern[1].strip("\"'")
                ignore = re.fullmatch(r'''    (?:ignore|"ignore"|'ignore')\s*:\s*(.*?)\s*''', line)
                # An empty scalar can introduce a following block list. Only
                # the explicit empty list is unambiguously exclusion-free.
                if ignore and ignore[1] != "[]":
                    ignored.add(current)
                if re.match(r"    <<\s*:", line):
                    # Inherited YAML fields may supply exclusions that this
                    # small stdlib parser cannot establish are empty.
                    ignored.add(current)
    collections = {name: path.expanduser().resolve()
                   for name, path in registered_collections(index).items()
                   if patterns.get(name) == "**/*.md" and name not in ignored}
    if _freshness_config(index)[1] != identity:
        return {}, ""
    return collections, identity


def _freshness_capture(index: str) -> tuple[dict[Path, int], str]:
    """Capture tracked generations only when QMD includes every dirty note.

    QMD 2.8.3 excludes hidden paths and common generated directories even with
    the full Markdown mask. A root containing an excluded tracked mutation
    cannot be acknowledged as a whole.
    """
    collections, identity = _freshness_collections(index)
    if not identity:
        return {}, ""
    from . import freshness
    captured = {}
    excluded_dirs = {"node_modules", ".git", ".cache", "vendor", "dist", "build"}
    for root in collections.values():
        if root in captured or not (root / ".claude-note/mutations/freshness/manifest.json").exists():
            continue
        try:
            value = freshness.snapshot(root)
            generation = value["saved_generation"]
            excluded = False
            for relative, entry in value["dirty"].items():
                path = Path(relative)
                if entry["generation"] > generation or path.suffix.lower() != ".md":
                    continue
                if (path.is_absolute() or ".." in path.parts
                        or any(part.startswith(".") for part in path.parts)
                        or any(part in excluded_dirs for part in path.parts[:-1])):
                    excluded = True
                    break
            if not excluded:
                captured[root] = generation
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _log(f"WARN cannot capture tracked freshness for {root}: {type(exc).__name__}")
    # registered_collections reads the config separately. It must still match
    # the configuration whose masks were inspected before capturing journals.
    if _freshness_config(index)[1] != identity:
        return {}, ""
    return captured, identity


def refresh(embed: bool = True, update_timeout: int = 900, embed_timeout: int = 3000) -> int:
    """`qmd update` then `qmd embed`, once at a time, bounded, with a heartbeat.

    A bare `qmd update` indexes text but never embeds: semantic search quietly dies
    while keyword search looks fine. Each stage is reported separately.
    """
    exe = qmd_exe()
    if not exe:
        _log("FAIL qmd not installed")
        return 1
    SHARE.mkdir(parents=True, exist_ok=True)
    try:
        LOCKDIR.mkdir()
    except FileExistsError:
        try:
            stale = time.time() - LOCKDIR.stat().st_mtime >= 90 * 60
        except OSError:
            stale = False
        if not stale:
            _log("SKIP another refresh is running")
            return 0
        _log("WARN clearing stale lock")
        try:
            LOCKDIR.rmdir()
            LOCKDIR.mkdir()
        except OSError:
            _log("SKIP refresh lock changed during stale recovery")
            return 0
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "CI": "1"}
    index = recall_mod.Settings.load().index
    base = [exe] + (["--index", index] if index else [])
    started = time.monotonic()
    lease = None
    update_ok, embed_ok = False, False
    try:
        # Capture before update: notes arriving during either stage may not be
        # present in its index, and must remain dirty for the following refresh.
        from . import index_refresh
        try:
            lease = index_refresh.begin(index)
            if lease.get("warning"):
                _log("WARN " + lease["warning"])
        except Exception as exc:  # metadata failure must not change indexing's exit status
            _log(f"WARN freshness capture unavailable: {type(exc).__name__}")
        for stage, timeout in (("update", update_timeout), ("embed", embed_timeout)):
            if stage == "embed" and not embed:
                continue
            try:
                proc = subprocess.run(base + [stage], capture_output=True, text=True,
                                      timeout=timeout, stdin=subprocess.DEVNULL, env=env)
            except subprocess.TimeoutExpired:
                _log(f"FAIL qmd {stage} timed out after {timeout}s")
                return 1
            if proc.returncode != 0:
                _log(f"FAIL qmd {stage} rc={proc.returncode}"
                     + (" (text index is current, vectors are stale)" if stage == "embed" else ""))
                return 1
            if stage == "update":
                update_ok = True
            else:
                embed_ok = True
        HEARTBEAT.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
        _log(f"OK {'update+embed' if embed else 'update'} in {time.monotonic() - started:.0f}s")
        _daily_eval()
        return 0
    finally:
        try:
            if lease is not None:
                try:
                    result = index_refresh.finish(lease["token"], lexical_ok=update_ok, vector_ok=embed_ok)
                    if result.get("warning"):
                        _log("WARN " + result["warning"])
                except Exception as exc:  # metadata failure must not change indexing's exit status
                    _log(f"WARN tracked freshness not acknowledged: {type(exc).__name__}")
        finally:
            try:
                LOCKDIR.rmdir()
            except OSError:
                pass


def _daily_eval() -> None:
    """Once a day, re-score the frozen probe set: free (local searches only) and it never
    changes a setting, so the dashboard's trend line fills itself. Failures are logged, not raised."""
    try:
        from . import evals
        if not evals.PROBES.exists():
            return
        runs = [r for r in evals.load_runs() if r.get("kind") == "retrieval"]
        if runs and time.time() - datetime.fromisoformat(runs[-1]["t"].replace("Z", "+00:00")).timestamp() < 20 * 3600:
            return
        run = evals.run_retrieval(evals.load_cases(evals.PROBES), label="daily")
        evals.save_run(run)
        m = run["metrics"].get("test") or run["metrics"]["all"]
        _log(f"EVAL right note injected {m.get('hit_at_k', 0):.0%} (test, {m.get('positives', 0)} questions)")
    except Exception as exc:  # noqa: BLE001 - the refresh already succeeded
        _log(f"EVAL skipped: {type(exc).__name__}")


def existing_refresh_job() -> Optional[Path]:
    """Another hourly qmd job already installed (ours or hand-made)."""
    agents = HOME / "Library" / "LaunchAgents"
    if not agents.is_dir():
        return None
    for p in agents.glob("*.plist"):
        if p.name == f"{PLIST_LABEL}.plist":
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if "qmd" in text and ("refresh" in text or "update" in text) and "claude-note.worker" not in text:
            return p
    return None


def install_refresh_agent(exe: str, dry_run: bool = False) -> str:
    if sys.platform != "darwin":
        return ("not macOS: schedule `claude-note refresh` hourly with cron or a systemd timer")
    other = existing_refresh_job()
    if other:
        return f"skipped: {other.name} already refreshes QMD"
    plist = HOME / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{PLIST_LABEL}</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>refresh</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>{SHARE}/refresh.err</string>
</dict></plist>
"""
    if dry_run:
        return f"would install {plist.name}"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(body)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)], capture_output=True, timeout=30)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, timeout=30)
    return f"installed {plist.name} (hourly)"


EVAL_LABEL = "com.claude-note.eval-nightly"


def install_eval_agent(exe: str, dry_run: bool = False, hour: int = 4, minute: int = 30) -> str:
    """Re-score recall every night (`claude-note eval nightly`): the frozen probes plus the
    off-topic set, the real-use log and the dashboard, with a regression check. Local searches
    only, no model calls; capped at an hour of wall clock."""
    if sys.platform != "darwin":
        return f"not macOS: run `{exe} eval nightly` daily from cron or a systemd timer"
    plist = HOME / "Library" / "LaunchAgents" / f"{EVAL_LABEL}.plist"
    timeout = shutil.which("timeout") or shutil.which("gtimeout")
    cmd = ([timeout, "-k", "60", "3600"] if timeout else []) + [exe, "eval", "nightly"]
    args = "".join(f"<string>{c}</string>" for c in cmd)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{EVAL_LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>WorkingDirectory</key><string>{SHARE}</string>
  <key>StandardOutPath</key><string>{SHARE}/eval-nightly.log</string>
  <key>StandardErrorPath</key><string>{SHARE}/eval-nightly.log</string>
</dict></plist>
"""
    if dry_run:
        return f"would install {plist.name} (daily {hour:02d}:{minute:02d})"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(body)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)], capture_output=True, timeout=30)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, timeout=30)
    return f"installed {plist.name} (daily {hour:02d}:{minute:02d}; results in `claude-note eval dashboard`)"


def qmd_major(exe: str) -> int:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"(\d+)\.\d+\.\d+", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def wrapper_targets(qmd: str) -> list[Path]:
    """Absolute paths a small shell wrapper execs. Installers often put a script such as
    `exec "/…/nvm/…/bin/node" "/…/node_modules/@tobilu/qmd/bin/qmd" "$@"` on PATH; the SDK
    and the node QMD's native modules were built for are where it points, not beside it."""
    try:
        with Path(qmd).resolve().open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return []
    if not head.startswith(b"#!") or b"\0" in head:
        return []
    out = []
    for line in head.decode("utf-8", "replace").splitlines()[1:]:
        if re.match(r"\s*exec\s", line):
            for raw in re.findall(r"""["']?(/[^\s"'$]+)["']?""", line):
                target = Path(raw)
                if target.is_file() and target not in out:
                    out.append(target)
    return out


def wrapper_node(qmd: str) -> Optional[str]:
    """The node a QMD wrapper runs, when it names one."""
    for target in wrapper_targets(qmd):
        if target.name == "node" and os.access(target, os.X_OK):
            return str(target)
    return None


def qmd_sdk(qmd: str) -> Optional[Path]:
    """The QMD 2.x SDK entry (dist/index.js) of the installed `qmd`, found from its executable
    or, for a wrapper script, from what it execs."""
    try:
        real = Path(qmd).resolve()
    except OSError:
        return None
    starts = [real] + [t.resolve() for t in wrapper_targets(qmd) if t.name != "node"]
    for parent in [p for start in starts for p in [start.parent, *start.parents][:6]]:
        for cand in (parent / "node_modules" / "@tobilu" / "qmd" / "dist" / "index.js", parent / "dist" / "index.js"):
            if cand.is_file() and (cand.parent / "llm.js").is_file():
                return cand
    return None


def real_node() -> str | None:
    """The node executable itself. nodenv, nvm and asdf put shims on PATH that pick a version
    from the login shell; under launchd that lookup fails and the agent exits 127. Ask node
    where it really lives, and record that."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "-p", "process.execPath"], capture_output=True, text=True, timeout=15)
        real = out.stdout.strip()
        if out.returncode == 0 and real and Path(real).is_file():
            return real
    except (OSError, subprocess.SubprocessError):
        pass
    return node


def _missing_server(result) -> bool:
    message = (result.stderr or result.stdout or '').casefold()
    return bool(result.returncode and 'could not find service' in message and SERVER_LABEL in message)


def _server_arguments(previous) -> list[str]:
    if not isinstance(previous, dict) or previous.get('Label') != SERVER_LABEL:
        raise ValueError('existing recall agent has an unsupported label or plist shape')
    arguments = previous.get('ProgramArguments')
    if (not isinstance(arguments, list) or not 3 <= len(arguments) <= 6
            or any(not isinstance(value, str) or not value or '\0' in value for value in arguments)
            or Path(arguments[1]).name != 'recall_server.mjs'):
        raise ValueError('existing recall agent has unsupported runtime arguments')
    if len(arguments) > 3 and (not arguments[3].isdigit() or not 1 <= int(arguments[3]) <= 65535):
        raise ValueError('existing recall agent has an invalid port')
    if previous.get('Program', arguments[0]) != arguments[0]:
        raise ValueError('existing recall agent uses an unsupported launcher')
    environment = previous.get('EnvironmentVariables', {})
    if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                               for key, value in environment.items()):
        raise ValueError('existing recall agent has an invalid environment')
    return arguments


def install_server_agent(qmd: str, port: int = 8182, dry_run: bool = False, *, previous=None) -> str:
    """Keep the recall server warm: QMD's keyword and meaning search plus its reranker, models
    loaded, on 127.0.0.1. Recall falls back to the CLI ladder whenever it is not answering."""
    if not qmd:
        return "skipped: qmd not installed"
    if qmd_major(qmd) < 2:
        return "skipped: QMD 2.x needed for search by meaning (`npm i -g @tobilu/qmd`); recall uses keywords only"
    sdk = qmd_sdk(qmd)
    node = wrapper_node(qmd) or real_node()
    if not sdk or not node:
        return "skipped: could not find QMD's SDK or node; recall uses keywords only"
    server = Path(__file__).with_name("recall_server.mjs")
    try:
        pin = re.search(r"(?m)^const PIN = ['\"]([^'\"]+)['\"];", server.read_text())
        with (sdk.parent.parent / 'package.json').open('rb') as handle:
            metadata = handle.read(16385)
        if len(metadata) > 16384 or not pin or json.loads(metadata).get('version') != pin[1]:
            return 'failed: installed QMD SDK does not match the bundled recall runtime version'
    except (OSError, ValueError, AttributeError):
        return 'failed: cannot verify the installed QMD SDK version before restarting recall'
    if sys.platform != "darwin":
        return f"not macOS: run `{node} {server} {sdk} {port}` under systemd (Restart=always)"
    plist = HOME / "Library" / "LaunchAgents" / f"{SERVER_LABEL}.plist"
    body = {"Label": SERVER_LABEL, "ProgramArguments": [node, str(server), str(sdk), str(port)],
            "EnvironmentVariables": {"QMD_METAL_KEEP_RESIDENCY": "1"}, "KeepAlive": True,
            "RunAtLoad": True, "ProcessType": "Interactive", "Nice": 0, "StandardOutPath": "/dev/null",
            "StandardErrorPath": str(SHARE / "recall-server.err")}
    if previous is not None:
        arguments = _server_arguments(previous)
        body.update(previous)
        body['ProgramArguments'] = [node, str(server), str(sdk), *(arguments[3:] or [str(port)])]
        body['EnvironmentVariables'] = {"QMD_METAL_KEEP_RESIDENCY": "1", **previous.get('EnvironmentVariables', {})}
        if 'Program' in previous:
            body['Program'] = node
        # A value matching an old installer default is still owner configuration.
        # Apply new resource defaults only to a new agent, preserving absence too.
        for key in ('Nice', 'ProcessType'):
            if key not in previous:
                body.pop(key, None)
    if dry_run:
        return f"would install {plist.name} (127.0.0.1:{port})"
    if plist.is_symlink():
        return "failed: recall agent plist is a symlink; reconcile it before upgrading"
    plist.parent.mkdir(parents=True, exist_ok=True)
    SHARE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=plist.parent, prefix=f'.{SERVER_LABEL}.', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            plistlib.dump(body, handle)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.replace(plist)
        finally:
            temporary.unlink(missing_ok=True)
    uid = os.getuid()
    target = f"gui/{uid}/{SERVER_LABEL}"
    stopped = subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)],
                             capture_output=True, text=True, timeout=30)
    if stopped.returncode:
        status = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, timeout=30)
        if not _missing_server(status):
            return f"failed: could not stop existing recall server: {(stopped.stderr or stopped.stdout).strip()[:200]}"
    started = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                             capture_output=True, text=True, timeout=30)
    if started.returncode:
        return f"failed: could not start recall server: {(started.stderr or started.stdout).strip()[:200]}"
    if subprocess.run(["launchctl", "print", target], capture_output=True, timeout=30).returncode:
        return "failed: recall server was not loaded after bootstrap"
    return f"installed {plist.name} (127.0.0.1:{port}, restarts if it stops)"


def upgrade_existing_server() -> int:
    """Installer-only refresh from this installed package; never set up a new job."""
    if sys.platform != 'darwin':
        return 0
    plist = HOME / 'Library/LaunchAgents' / f'{SERVER_LABEL}.plist'
    try:
        status = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{SERVER_LABEL}'],
                                capture_output=True, text=True, timeout=30)
        loaded = status.returncode == 0
        if not loaded and not _missing_server(status):
            raise ValueError('cannot determine recall service state: ' + (status.stderr or status.stdout).strip()[:200])
        if not plist.exists() and not plist.is_symlink() and not loaded:
            print('Recall server not configured; left absent')
            return 0
        if not plist.is_file() or plist.is_symlink():
            raise ValueError('existing recall service has no regular plist; restore its configuration before upgrading')
        port = 8182
        with plist.open('rb') as handle:
            previous = handle.read(65537)
        if len(previous) > 65536:
            raise ValueError('existing recall agent plist exceeds inspection bound')
        previous = plistlib.loads(previous)
        arguments = _server_arguments(previous)
        if len(arguments) > 3:
            port = int(arguments[3])
        verdict = install_server_agent(qmd_exe() or '', port=port, previous=previous)
        print('Recall server upgrade: ' + verdict)
        return 0 if verdict.startswith('installed ') else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Recall server upgrade failed: {exc}', file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- #
# setup command
# --------------------------------------------------------------------------- #


def cmd_setup(args) -> int:
    exe = shutil.which("claude-note") or str(HOME / ".local" / "bin" / "claude-note")
    dry = args.dry_run
    print("claude-note setup" + ("  (dry run: nothing is written)" if dry else ""))

    print("\n1. One index across your knowledge")
    try:
        from . import config
        vault = config.VAULT_ROOT
    except SystemExit:
        print("   no vault configured; run the installer first")
        return 1
    print(f"   vault {vault}: {add_collection(vault, args.vault_name, '**/*.md', dry_run=dry)}")
    memory_collections: list[str] = []
    if not args.no_claude_memory:
        projects = HOME / ".claude" / "projects"
        if projects.is_dir():
            verdict = add_collection(projects, "agentmem", "**/memory/*.md", dry_run=dry)
            print(f"   Claude Code memories {projects}: {verdict}")
            name = next((n for n, p in registered_collections().items() if p.resolve() == projects.resolve()), "agentmem")
            memory_collections.append(name)
    for extra in args.add or []:
        p = Path(extra).expanduser()
        print(f"   {p}: {add_collection(p, slugify_name(p), '**/*.md', dry_run=dry)}")
    print(f"   public recall canary: {install_public_canary(dry_run=dry)}")

    print("\n2. Recall and sentinel hooks")
    claude_settings = Path(args.claude_settings).expanduser()
    added = install_hooks(claude_settings, exe, dry_run=dry)
    print(f"   Claude Code ({claude_settings}): " + (", ".join(added) if added else "already installed"))
    if args.codex:
        codex_hooks = Path(args.codex_hooks).expanduser()
        added = install_hooks(codex_hooks, exe, codex=True, dry_run=dry)
        print(f"   Codex ({codex_hooks}): " + (", ".join(added) if added else "already installed"))
        print("   Codex runs new hooks only after you trust them (`codex`, accept the prompt).")

    print("\n3. Recall rule in agent instructions")
    if not args.no_rules:
        print(f"   ~/.claude/CLAUDE.md: {install_rule(HOME / '.claude' / 'CLAUDE.md', dry)}")
        if args.codex:
            print(f"   ~/.codex/AGENTS.md: {install_rule(HOME / '.codex' / 'AGENTS.md', dry)}")
    else:
        print("   skipped (--no-rules)")

    print("\n4. Config")
    print(f"   {recall_mod._config_path()}: {ensure_recall_config(memory_collections, dry)}")

    print("\n5. Hourly index refresh")
    if args.no_schedule:
        print("   skipped (--no-schedule): run `claude-note refresh` from your own scheduler")
    else:
        print(f"   {install_refresh_agent(exe, dry)}")

    print("\n6. Warm recall server (search by meaning + reranker, QMD 2.x)")
    if args.no_schedule:
        print("   skipped (--no-schedule)")
    else:
        print(f"   {install_server_agent(qmd_exe() or '', dry_run=dry)}")

    print("\n7. Nightly evals (is recall still finding the right notes?)")
    if args.no_schedule:
        print("   skipped (--no-schedule)")
    else:
        print(f"   {install_eval_agent(exe, dry)}")

    print("\nNext: `claude-note doctor` proves the loop end to end.")
    return 0


def slugify_name(path: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-") or "notes"


def install_public_canary(*, dry_run: bool = False) -> str:
    """Setup-only registration of bundled synthetic text in the default index."""
    directory = HOME / ".local/share/claude-note/public-canary"
    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "doctor-canary.md").write_text(CANARY, encoding="utf-8")
        (directory / "diagnostic-context.md").write_text("---\ntype: reference\n---\n# Payment retry diagnostics\n\n"
            "Customers are charged twice when a payment request is retried. Provider logs help diagnose the sequence, "
            "but logging alone does not prevent duplicate charges. Verify the logical operation identity before a retry.\n", encoding="utf-8")
    return add_collection(directory, PUBLIC_CANARY_COLLECTION, "*.md", dry_run=dry_run)


def probe_default_server(settings=None, *, save: bool = True) -> dict:
    """Read-only corpus probe through the default warm server, separate from CLI canary."""
    settings = settings or recall_mod.Settings.load()
    manifest = recall_mod.server_capabilities(settings, timeout=2) or {}
    checks = []
    expected = f"qmd://{PUBLIC_CANARY_COLLECTION}/doctor-canary.md"
    public = replace(settings, engine="server", index="", collections=[PUBLIC_CANARY_COLLECTION],
                     memory_collections=[], log=False, budget=min(settings.budget, 8.0))
    result = recall_mod.recall("Why are customers charged twice when a payment request is retried?", public)
    manifest = recall_mod.server_capabilities(settings, timeout=2) or manifest
    for name, key, enabled in (("lexical readiness", "lexical_ready", True),
                               ("vector readiness", "vector_ready", settings.vector),
                               ("rerank readiness", "rerank_ready", settings.rerank)):
        checks.append({"check": name, "ok": manifest.get(key) is True or not enabled,
                       "detail": "observed server capability; not a quality score" if enabled else "disabled in recall settings"})
    # Global readiness may describe another request. Require this public query's
    # positive scoring observation separately; neither is native execution proof.
    scored = result.reranked is True and result.coverage.get("rerank") == "exact_passage"
    checks.append({"check": "current public query rerank", "ok": scored or not settings.rerank,
                   "detail": ("validated non-fallback passage scoring for this query; not native execution proof" if scored
                              else "this query did not produce validated non-fallback passage scores")
                   if settings.rerank else "disabled in recall settings"})
    found = result.engine == "server" and any(h.uri == expected for h in result.hits)
    checks.append({"check": "default server retrieves public canary", "ok": found,
                   "detail": "exact scoped public source returned" if found else result.reason or
                   "public canary missing; run setup and refresh (doctor does not alter the index)"})
    out = {"schema_version": 2, "t": datetime.now(timezone.utc).isoformat(),
           "manifest": manifest, "route": "default-server", "corpus": "bundled-public-synthetic",
           "checks": checks, "ok": bool(manifest) and all(c["ok"] for c in checks)}
    if save:
        CAPABILITY_RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".capability-", dir=CAPABILITY_RECEIPT.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(out, stream, sort_keys=True)
            Path(name).replace(CAPABILITY_RECEIPT)
        finally:
            Path(name).unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #

CANARY = """---
title: "Doctor canary: retried payments double-charge"
type: gotcha
---
# Doctor canary: retried payments double-charge

Customers were charged twice when a payment request was retried after a timeout:
the client minted a new idempotency key on every attempt, so the server saw two
payments. Mint the key once per payment and reuse it on every retry.
"""


def _hook(exe: list[str], which: str, payload: dict, env: dict) -> str:
    proc = subprocess.run(exe + ["hook", which], input=json.dumps(payload), capture_output=True,
                          text=True, timeout=30, env=env)
    try:
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        return ""


def probe_loop(exe: list[str]) -> list[tuple[str, bool, str]]:
    """Run the real hook commands against a throwaway index holding one canary."""
    q = qmd_exe()
    if not q:
        return [("qmd installed", False, "install https://github.com/tobi/qmd")]
    index = f"claude-note-doctor-{os.getpid()}"
    tmp = Path(tempfile.mkdtemp(prefix="claude-note-doctor-"))
    checks: list[tuple[str, bool, str]] = []
    try:
        vault = tmp / "vault"
        vault.mkdir()
        (vault / "doctor-canary.md").write_text(CANARY)
        subprocess.run([q, "--index", index, "collection", "add", str(vault), "--name", "canary",
                        "--mask", "**/*.md"], capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL)
        env = {**os.environ, "CLAUDE_NOTE_RECALL_INDEX": index, "CLAUDE_NOTE_RECALL": "on",
               "CLAUDE_NOTE_RECALL_LOG": "0"}
        t0 = time.monotonic()
        ctx = _hook(exe, "recall", {"hook_event_name": "UserPromptSubmit", "prompt":
                    "Why are customers charged twice when a payment request is retried?", "cwd": str(tmp)}, env)
        ms = (time.monotonic() - t0) * 1000
        checks.append(("recall hook injects a matching note", "Doctor canary" in ctx,
                       f"{ms:.0f} ms end to end" if ctx else "no additionalContext returned"))
        ctx = _hook(exe, "recall", {"hook_event_name": "UserPromptSubmit",
                    "prompt": "Configure the zeppelin hangar lighting schedule", "cwd": str(tmp)}, env)
        checks.append(("recall announces a miss instead of staying silent",
                       "found nothing" in ctx, "miss notice present" if ctx else "silent on a miss"))
        payload = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                   "session_id": f"doctor-{os.getpid()}-{time.time()}",
                   "tool_input": {"command": "python pay.py --retry"},
                   "error": "Exit code 1\nAssertionError: customer charged twice after payment retry"}
        ctx = _hook(exe, "sentinel", payload, env)
        checks.append(("sentinel surfaces the known fix for a failed command",
                       "Doctor canary" in ctx, "fix recalled" if ctx else "nothing surfaced"))
        quiet = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": payload["session_id"] + "q",
                 "tool_input": {"command": "grep -n 'invalid_grant|401' sentinel.py"},
                 "tool_response": {"stdout": "41: invalid_grant|401 Unauthorized", "stderr": ""}}
        ctx = _hook(exe, "sentinel", quiet, env)
        checks.append(("sentinel ignores error words in successful output", ctx == "",
                       "quiet" if not ctx else "fired on a grep"))
    finally:
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(HOME / ".cache"))) / "qmd"
        for p in cache.glob(f"{index}.sqlite*"):
            p.unlink(missing_ok=True)
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config"))) / "qmd" / f"{index}.yml"
        cfg.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return checks


def cmd_doctor(args) -> int:
    exe = [sys.executable, "-m", "claude_note"]
    rows: list[tuple[str, str, bool, str]] = []

    q = qmd_exe()
    rows.append(("configured", "qmd installed", bool(q), q or "missing"))
    cols = registered_collections()
    rows.append(("configured", "collections indexed", bool(cols), ", ".join(cols) or "none"))
    for label, path in (("Claude Code", Path(args.claude_settings).expanduser()),
                        ("Codex", Path(args.codex_hooks).expanduser())):
        if not path.exists():
            continue
        st = hooks_status(path)
        parts = [k for k, v in st.items() if v]
        ok = st["recall"] and (st["sentinel"] or label == "Codex")
        rows.append(("configured", f"{label} hooks", ok, ", ".join(parts) or "none"))
    age = None
    for hb in (HEARTBEAT, Path("/var/tmp/heartbeats/qmd-refresh.ok")):
        if hb.exists():
            age = (time.time() - hb.stat().st_mtime) / 3600
            break
    settings = recall_mod.Settings.load()
    vault_setting = os.environ.get("CLAUDE_NOTE_VAULT") or recall_mod._load_toml().get("vault_root")
    if vault_setting and Path(vault_setting).expanduser().is_dir():
        from .health import freshness_summary
        freshness = freshness_summary(Path(vault_setting).expanduser())
        for capability in ("lexical", "vector"):
            current = freshness.get(capability + "_fresh")
            rows.append(("configured freshness", capability + " generation", current is True,
                         f"{freshness.get(capability + '_generation', 'unknown')} / saved {freshness.get('saved_generation', 'unknown')}; tracked mutations only"))
    if settings.engine != "cli":
        alive = recall_mod.server_alive(settings, timeout=2)
        rows.append(("configured", "warm recall server (meaning + rerank)", alive or settings.engine == "auto",
                     f"{settings.server_url} answering" if alive else
                     "not answering: recall uses keywords only (`claude-note setup` installs it)"))
    rows.append(("configured", "index refreshed recently", age is not None and age < 3,
                 f"{age:.1f} h ago" if age is not None else "no refresh heartbeat"))

    if not args.skip_probe:
        capabilities = probe_default_server(settings)
        for check in capabilities["checks"]:
            rows.append(("exercised default server", check["check"], check["ok"], check["detail"]))
        for name, ok, detail in probe_loop(exe):
            rows.append(("exercised isolated CLI", name, ok, detail))

    st = recall_mod.stats(7)
    prompts = st["by_kind"].get("prompt", {})
    total = sum(prompts.values())
    if total:
        hit = prompts.get("hit", 0)
        rows.append(("in use", "recall on real prompts, 7 days", True,
                     f"{total} prompts, {hit} with notes ({100 * hit / total:.0f}%), p50 {st['p50_ms']} ms"))
        fail = st["by_kind"].get("failure", {})
        if fail:
            rows.append(("in use", "failures with a known fix, 7 days", True,
                         f"{sum(fail.values())} surfaced"))
    else:
        rows.append(("in use", "recall on real prompts, 7 days", False, "no recall activity logged yet"))

    # Measured, not just used: the latest labelled eval run (see docs/evals.md).
    from . import evals
    runs = [r for r in evals.load_runs() if r.get("kind") == "retrieval"]
    if runs:
        last = runs[-1]
        m = last["metrics"].get("test") or last["metrics"].get("all") or {}
        days = (time.time() - datetime.fromisoformat(last["t"].replace("Z", "+00:00")).timestamp()) / 86400
        rows.append(("in use", "eval: right note injected (test split)", days < 8,
                     f"{m.get('hit_at_k', 0):.0%} of {m.get('positives', 0)} questions, {days:.0f} d ago"
                     + ("" if days < 8 else "; run `claude-note eval run`")))
    else:
        rows.append(("in use", "eval: right note injected", False,
                     "never measured; `claude-note eval probes` then `eval run`"))
    try:
        night = json.loads(evals.NIGHTLY.read_text())
        age = (time.time() - datetime.fromisoformat(night["t"].replace("Z", "+00:00")).timestamp()) / 86400
        detail = (night.get("skipped") or night.get("error") or
                  ("no regression" if night.get("ok") else "REGRESSION: " + "; ".join(night.get("regressions", []))))
        rows.append(("in use", "nightly eval", age < 2 and night.get("ok", False),
                     f"{detail}, {age:.0f} d ago" + ("" if age < 2 else "; is the nightly agent running?")))
    except (OSError, ValueError, KeyError):
        rows.append(("in use", "nightly eval", False, "not run yet; `claude-note setup` schedules it"))

    if args.json:
        print(json.dumps([{"stage": s, "check": n, "ok": ok, "detail": d} for s, n, ok, d in rows], indent=2))
    else:
        stage = None
        for s, name, ok, detail in rows:
            if s != stage:
                print(f"\n{s}")
                stage = s
            print(f"  {'✓' if ok else '✗'} {name:<52} {detail}")
        print()
    return 0 if all(ok for s, _, ok, _ in rows if s != "in use") else 1
