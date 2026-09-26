"""
Pick up sessions from assistants that have no usable transcript hook.

- Cursor: chats live in its `state.vscdb` SQLite (read-only here).
- Claude.ai / ChatGPT "Export data" zips dropped in ~/Downloads or
  ~/Documents/claude-note-imports.
- Codex / ChatGPT-app rollouts whose hook has not fired (hooks are skipped
  until trusted), so capture works before that one-time step.

Each conversation is written as a Claude-Code-shaped JSONL transcript under
~/.local/share/claude-note/transcripts (local only) and queued as a
UserPromptSubmit + Stop pair, so the normal worker writes the note and runs
synthesis. A content hash per conversation makes every source idempotent.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import models, queue_manager

HOME = Path.home()
TRANSCRIPTS_DIR = HOME / ".local/share/claude-note/transcripts"
SEEN_FILE = TRANSCRIPTS_DIR / "seen.json"
HEARTBEAT = HOME / "Library/Logs/claude-note/import-sweep.ok"
IMPORTS_DIR = HOME / "Documents/claude-note-imports"
WATCH_DIRS = [HOME / "Downloads", IMPORTS_DIR]
CURSOR_DB = HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
CURSOR_WORKSPACES = HOME / "Library/Application Support/Cursor/User/workspaceStorage"
CODEX_SESSIONS = HOME / ".codex/sessions"

SWEEP_INTERVAL = 1800           # seconds between sweeps inside the worker
FIRST_RUN_LOOKBACK_DAYS = 7     # first sweep does not backfill all history
CODEX_IDLE_SECONDS = 600        # a rollout untouched this long is finished


# --------------------------------------------------------------------------
# Shared: seen-store, transcript writer
# --------------------------------------------------------------------------

def _load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_seen(seen: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=1, sort_keys=True))
    tmp.replace(SEEN_FILE)


def _since(seen: dict, since: Optional[datetime]) -> float:
    """Oldest update time (epoch) worth importing; set once on first run."""
    if since is not None:
        return since.timestamp()
    if "_watermark" not in seen:
        seen["_watermark"] = time.time() - FIRST_RUN_LOOKBACK_DAYS * 86400
    return float(seen["_watermark"])


def _enqueue(session_id: str, transcript_path: Path, cwd: str, prompt: str, source: str) -> None:
    for event in ("UserPromptSubmit", "Stop"):
        queue_manager.enqueue_event(models.QueuedEvent.from_hook_input({
            "hook_event_name": event,
            "session_id": session_id,
            "cwd": cwd,
            "transcript_path": str(transcript_path),
            "prompt": prompt[:500] if event == "UserPromptSubmit" else "",
            "source": source,
        }))


def write_session(seen: dict, source: str, conv_id: str, messages: list, cwd: str = "") -> bool:
    """Write one conversation as a transcript and queue it, unless unchanged.

    `messages` are dicts: {"role": "user"|"assistant", "text": str,
    "tools": [{"name", "input", "output"}], "thinking": [str]}.
    """
    messages = [m for m in messages if (m.get("text") or "").strip() or m.get("tools")]
    prompts = [m["text"].strip() for m in messages if m["role"] == "user" and (m.get("text") or "").strip()]
    if not prompts or not any(m["role"] == "assistant" for m in messages):
        return False

    session_id = re.sub(r"[^A-Za-z0-9_-]", "", conv_id)[:80] or hashlib.sha256(conv_id.encode()).hexdigest()[:32]
    digest = hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest()
    if seen.get(f"{source}:{session_id}") == digest:
        return False

    path = TRANSCRIPTS_DIR / source / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for n, m in enumerate(messages):
        if m["role"] == "user":
            lines.append({"type": "user", "message": {"content": m.get("text", "")}})
            continue
        blocks = [{"type": "thinking", "thinking": t} for t in m.get("thinking") or [] if t]
        if (m.get("text") or "").strip():
            blocks.append({"type": "text", "text": m["text"]})
        results = []
        for i, tool in enumerate(m.get("tools") or []):
            tool_id = f"t{n}-{i}"
            tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {"input": tool.get("input")}
            blocks.append({"type": "tool_use", "id": tool_id, "name": tool.get("name") or "tool", "input": tool_input})
            if tool.get("output"):
                results.append({"type": "tool_result", "tool_use_id": tool_id, "content": str(tool["output"])})
        lines.append({"type": "assistant", "message": {"content": blocks}})
        lines.extend(results)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(line) + "\n" for line in lines))
    tmp.replace(path)

    _enqueue(session_id, path, cwd, prompts[0], source)
    seen[f"{source}:{session_id}"] = digest
    return True


# --------------------------------------------------------------------------
# Export zips (Claude.ai and ChatGPT "Export data")
# --------------------------------------------------------------------------

def _claude_export_messages(conv: dict) -> list:
    out = []
    for msg in conv.get("chat_messages") or []:
        role = "user" if msg.get("sender") == "human" else "assistant"
        texts = [b.get("text", "") for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
        thinking = [b.get("thinking", "") for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "thinking"]
        tools = [{"name": b.get("name"), "input": b.get("input")} for b in msg.get("content") or []
                 if isinstance(b, dict) and b.get("type") == "tool_use"]
        text = "\n\n".join(t for t in texts if t) or msg.get("text") or ""
        out.append({"role": role, "text": text, "tools": tools, "thinking": thinking})
    return out


def _chatgpt_export_messages(conv: dict) -> list:
    """Walk the mapping from current_node back to the root (the branch shown)."""
    mapping = conv.get("mapping") or {}
    node_id, chain = conv.get("current_node"), []
    while node_id and node_id in mapping and len(chain) < 100000:
        chain.append(mapping[node_id])
        node_id = mapping[node_id].get("parent")
    out = []
    for node in reversed(chain):
        msg = node.get("message") or {}
        role = (msg.get("author") or {}).get("role")
        content = msg.get("content") or {}
        if role not in ("user", "assistant") or (msg.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        if content.get("content_type") not in ("text", "multimodal_text"):
            continue
        text = "\n".join(p for p in content.get("parts") or [] if isinstance(p, str)).strip()
        if text:
            out.append({"role": role, "text": text})
    return out


def import_zip(zip_path: Path, seen: dict) -> Optional[int]:
    """Import one export zip. Returns conversations queued, or None if not an export."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist()
                       if re.fullmatch(r"(?:[^/]+/)?conversations(?:-\d+)?\.json", n)]
            if not members:
                return None
            queued = 0
            for name in members:
                convs = json.loads(zf.read(name))
                for conv in convs if isinstance(convs, list) else []:
                    if "chat_messages" in conv:
                        source, conv_id, msgs = "claude-ai", conv.get("uuid", ""), _claude_export_messages(conv)
                        cwd = "claude.ai"
                    elif "mapping" in conv:
                        source, conv_id, msgs = "chatgpt", conv.get("conversation_id") or conv.get("id", ""), _chatgpt_export_messages(conv)
                        cwd = "chatgpt.com"
                    else:
                        continue
                    if conv_id and write_session(seen, source, conv_id, msgs, cwd):
                        queued += 1
            return queued
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


def sweep_zips(seen: dict, problems: list) -> int:
    """Import export zips from the watched folders and file them under done/."""
    done_dir = IMPORTS_DIR / "done"
    queued = 0
    for folder in WATCH_DIRS:
        try:
            candidates = sorted(folder / n for n in os.listdir(folder) if n.endswith(".zip"))
        except FileNotFoundError:
            continue
        except PermissionError as e:
            # macOS privacy: the worker's Python needs access to this folder.
            problems.append(f"cannot read {folder}: {e.strerror}")
            continue
        for zip_path in candidates:
            if folder != IMPORTS_DIR and not _looks_like_export(zip_path):
                continue
            if time.time() - zip_path.stat().st_mtime < 60:
                continue  # still downloading
            count = import_zip(zip_path, seen)
            if count is None:
                continue
            queued += count
            done_dir.mkdir(parents=True, exist_ok=True)
            target = done_dir / zip_path.name
            if target.exists():
                target = done_dir / f"{zip_path.stem}-{int(time.time())}.zip"
            shutil.move(str(zip_path), target)
    return queued


def _looks_like_export(zip_path: Path) -> bool:
    """Claude: data-*.zip; ChatGPT: <64 hex>-<date>.zip. Content is checked after."""
    name = zip_path.name
    return name.startswith("data-") or bool(re.match(r"[0-9a-f]{64}-\d{4}-\d{2}-\d{2}", name))


# --------------------------------------------------------------------------
# Cursor
# --------------------------------------------------------------------------

def _cursor_workspace_folders() -> dict:
    """composerId -> workspace folder, from each workspace's own state.vscdb."""
    folders = {}
    for ws in CURSOR_WORKSPACES.glob("*"):
        try:
            folder = json.loads((ws / "workspace.json").read_text()).get("folder", "")
            db = sqlite3.connect(f"file:{ws / 'state.vscdb'}?mode=ro", uri=True, timeout=5)
            row = db.execute("select value from ItemTable where key='composer.composerData'").fetchone()
            db.close()
        except (OSError, ValueError, sqlite3.Error):
            continue
        for comp in (json.loads(row[0]) if row and row[0] else {}).get("allComposers") or []:
            if comp.get("composerId"):
                folders[comp["composerId"]] = folder.replace("file://", "")
    return folders


def _cursor_bubble_message(bubble: dict) -> Optional[dict]:
    role = {1: "user", 2: "assistant"}.get(bubble.get("type"))
    if not role:
        return None
    tools = []
    tf = bubble.get("toolFormerData") or {}
    if tf.get("name"):
        try:
            params = json.loads(tf.get("params") or tf.get("rawArgs") or "{}")
        except ValueError:
            params = {"input": tf.get("rawArgs")}
        params = params if isinstance(params, dict) else {"input": params}
        path = params.get("relativeWorkspacePath") or params.get("targetFile")
        if path and "file_path" not in params:
            params["file_path"] = path
        tools.append({"name": tf["name"], "input": params,
                      "output": (tf.get("result") or "")[:2000]})
    thinking = (bubble.get("thinking") or {}).get("text") if isinstance(bubble.get("thinking"), dict) else None
    return {"role": role, "text": bubble.get("text") or "", "tools": tools, "thinking": [thinking] if thinking else []}


def sweep_cursor(seen: dict, since_ts: float, db_path: Path = None) -> int:
    db_path = db_path or CURSOR_DB
    if not db_path.exists():
        return 0
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        folders = None
        queued = 0
        for key, value in db.execute("select key, value from cursorDiskKV where key like 'composerData:%'"):
            try:
                comp = json.loads(value) if value else None
            except ValueError:
                continue
            if not comp or (comp.get("lastUpdatedAt") or comp.get("createdAt") or 0) / 1000 < since_ts:
                continue
            comp_id = comp.get("composerId") or key.split(":", 1)[1]
            if comp.get("conversation"):
                bubbles = comp["conversation"]
            else:
                bubbles = []
                for header in comp.get("fullConversationHeadersOnly") or []:
                    row = db.execute("select value from cursorDiskKV where key=?",
                                     (f"bubbleId:{comp_id}:{header.get('bubbleId')}",)).fetchone()
                    if row and row[0]:
                        try:
                            bubbles.append(json.loads(row[0]))
                        except ValueError:
                            pass
            msgs = [m for m in (_cursor_bubble_message(b) for b in bubbles) if m]
            if folders is None:
                folders = _cursor_workspace_folders()
            if write_session(seen, "cursor", comp_id, msgs, folders.get(comp_id) or "cursor"):
                queued += 1
        return queued
    finally:
        db.close()


# --------------------------------------------------------------------------
# Codex rollouts not delivered by the hook
# --------------------------------------------------------------------------

def sweep_codex(seen: dict, since_ts: float, sessions_dir: Path = None) -> int:
    from .transcript_reader import codex_session_meta, read_transcript
    from . import session_tracker

    sessions_dir = sessions_dir or CODEX_SESSIONS
    queued = 0
    now = time.time()
    for path in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
        mtime = path.stat().st_mtime
        if mtime < since_ts or now - mtime < CODEX_IDLE_SECONDS:
            continue
        key = f"codex:{path.name}"
        if seen.get(key) == mtime:
            continue
        meta = codex_session_meta(path)
        session_id = meta.get("id")
        if not session_id or meta.get("background"):
            seen[key] = mtime
            continue
        if key not in seen and session_tracker.get_state_file(session_id).exists():
            seen[key] = mtime  # the hook already delivered this session
            continue
        content = read_transcript(path)
        if content.user_prompts and content.assistant_texts:
            _enqueue(session_id, path, meta.get("cwd", ""), content.user_prompts[0], "codex")
            queued += 1
        seen[key] = mtime
    return queued


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def sweep(since: Optional[datetime] = None, zips: Iterable[Path] = ()) -> dict:
    """Run every source once; touch the heartbeat on success."""
    seen = _load_seen()
    since_ts = _since(seen, since)
    counts, problems = {}, []
    for name, fn in (("zips", lambda: sweep_zips(seen, problems)),
                     ("cursor", lambda: sweep_cursor(seen, since_ts)),
                     ("codex", lambda: sweep_codex(seen, since_ts))):
        try:
            counts[name] = fn()
        except Exception as e:  # one broken source must not stop the others
            counts[name] = f"error: {e}"
        _save_seen(seen)
    if problems:
        counts["problems"] = problems
    for zip_path in zips:
        counts[str(zip_path)] = import_zip(Path(zip_path), seen)
        _save_seen(seen)
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(json.dumps({"ts": datetime.now().isoformat(), "counts": counts}))
    return counts
