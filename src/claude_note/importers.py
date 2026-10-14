"""
Pick up sessions from assistants that have no usable transcript hook.

- Cursor: chats live in its `state.vscdb` SQLite (read-only here).
- Claude.ai / ChatGPT "Export data" zips dropped in ~/Downloads or
  ~/Documents/claude-note-imports.
- Codex / ChatGPT-app rollouts whose hook has not fired (hooks are skipped
  until trusted), so capture works before that one-time step.
- Claude Code sessions the hook never saw: those from before the hooks were
  installed (the first sweep looks back FIRST_RUN_LOOKBACK_DAYS, `import --since`
  further) and those from a Claude Code whose settings lacked the hook.

Each conversation is written as a Claude-Code-shaped JSONL transcript under
~/.local/share/claude-note/transcripts (local only) and queued as a
UserPromptSubmit + Stop pair, so the normal worker writes the note and runs
synthesis. A content hash per conversation makes every source idempotent.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


HOME = Path.home()
TRANSCRIPTS_DIR = HOME / ".local/share/claude-note/transcripts"
SEEN_FILE = TRANSCRIPTS_DIR / "seen.json"
HEARTBEAT = HOME / "Library/Logs/claude-note/import-sweep.ok"
IMPORTS_DIR = HOME / "Documents/claude-note-imports"
WATCH_DIRS = [HOME / "Downloads", IMPORTS_DIR]
CURSOR_DB = HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
CURSOR_WORKSPACES = HOME / "Library/Application Support/Cursor/User/workspaceStorage"
CODEX_SESSIONS = HOME / ".codex/sessions"
CLAUDE_PROJECTS = HOME / ".claude/projects"

SWEEP_INTERVAL = 1800           # seconds between sweeps inside the worker
FIRST_RUN_LOOKBACK_DAYS = 7     # first sweep does not backfill all history
CODEX_IDLE_SECONDS = 600        # a rollout untouched this long is finished
CLAUDE_CODE_MAX_PER_SWEEP = 10  # each queued session costs one synthesis call; the rest waits a sweep
MAX_IMPORT_PROBLEM_CHARS = 768
MAX_IMPORT_PROBLEM_DETAILS = 100
MAX_IMPORT_PAYLOAD_BYTES = 256 * 1024 * 1024


class ImportProblems(list):
    """Bound displayed diagnostics without losing the exact failure count."""
    def __init__(self, limit=MAX_IMPORT_PROBLEM_DETAILS):
        if type(limit) is not int or not 1 <= limit <= MAX_IMPORT_PROBLEM_DETAILS:
            raise ValueError('Import problem detail limit must be between 1 and 100')
        super().__init__()
        self.limit = limit
        self.total = 0

    @property
    def omitted(self):
        return self.total - len(self)

    def append(self, detail):
        self.total += 1
        if len(self) < self.limit:
            super().append(detail[:MAX_IMPORT_PROBLEM_CHARS])

    def extend(self, details):
        total = getattr(details, 'total', len(details))
        self.total += total
        for detail in details:
            if len(self) >= self.limit:
                break
            super().append(detail[:MAX_IMPORT_PROBLEM_CHARS])


def _counter(report, key, count=1):
    if report is not None:
        report[key] = report.get(key, 0) + count


class SourceImportError(RuntimeError):
    """Direct callers without a problem sink still receive visible failure."""
    def __init__(self, source, queued, problems, report=None):
        self.queued = queued
        self.problems = tuple(problems)
        self.failed = getattr(problems, 'total', len(problems))
        self.omitted_problem_details = getattr(problems, 'omitted', 0)
        self.report = dict(report or {})
        super().__init__(f'{source}: {self.failed} failed item(s); {queued} queued; {problems[0]}'[:1024])


def _source_problem(source, path, error):
    from .transcript_reader import TRANSCRIPT_LIMIT_ERROR, METADATA_LIMIT_ERROR
    safe_messages = {TRANSCRIPT_LIMIT_ERROR, METADATA_LIMIT_ERROR,
        'Transcript source must be a regular file', 'Codex session metadata is missing or malformed',
        'Claude Code session metadata is malformed',
        'Transcript contains malformed or incomplete JSONL records',
        'Export member exceeds 256 MiB; preserve the original for lossless recovery',
        'Export member must contain a conversation list', 'Export conversation is malformed',
        'Export conversation identity is missing or malformed', 'Export parent chain is incomplete or cyclic',
        'Cursor composer is malformed', 'Cursor conversation is missing or malformed',
        'Cursor required bubble is missing or malformed', 'Cursor bubble type is unsupported',
        'Cursor workspace metadata is malformed', 'Cursor source exceeds 256 MiB'}
    message = error.args[0] if len(error.args) == 1 and isinstance(error.args[0], str) else ''
    detail = ': ' + message if message in safe_messages else ''
    # Exception messages can contain source text. Report type and an escaped,
    # bounded path, plus only fixed explanations authored in this package.
    label = json.dumps(str(path), ensure_ascii=True)
    if len(label) > 500:
        label = label[:450] + '...#' + hashlib.sha256(str(path).encode('utf-8', 'surrogatepass')).hexdigest()[:16]
    return f'{source}: {label}: {type(error).__name__}{detail}'[:MAX_IMPORT_PROBLEM_CHARS]


def _finish_source(source, queued, errors, problems, report=None):
    if report is not None:
        report['queued_sessions'] = queued
        report['failed_items'] = getattr(errors, 'total', len(errors))
        report['problem_details_omitted'] = getattr(errors, 'omitted', 0)
    if problems is not None:
        problems.extend(errors)
        if not isinstance(problems, ImportProblems) and getattr(errors, 'omitted', 0):
            problems.append(f'{source}: {errors.omitted} additional failures; details omitted')
    elif errors:
        raise SourceImportError(source, queued, errors, report)
    return queued


def _validate_native_content(content):
    if content.coverage.get('malformed_records') or content.coverage.get('incomplete_trailing_bytes'):
        raise ValueError('Transcript contains malformed or incomplete JSONL records')


# --------------------------------------------------------------------------
# Shared: seen-store, transcript writer
# --------------------------------------------------------------------------

def _load_seen() -> dict:
    try:
        value = json.loads(SEEN_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_seen(seen: dict) -> None:
    from .state_io import atomic_json
    atomic_json(SEEN_FILE, seen)


def _since(seen: dict, since: Optional[datetime]) -> float:
    """Oldest update time (epoch) worth importing; set once on first run."""
    if since is not None:
        return since.timestamp()
    if "_watermark" not in seen:
        seen["_watermark"] = time.time() - FIRST_RUN_LOOKBACK_DAYS * 86400
    return float(seen["_watermark"])


def _enqueue(session_id: str, transcript_path: Path, cwd: str, prompt: str, source: str,
             *, raw: bytes, logical_id=None, logical_revision=None, origin_sources=()) -> bool:
    """Publish one captured revision as a complete, retry-stable local batch."""
    from . import import_publication
    result = import_publication.publish(source, session_id,
        logical_id if logical_id is not None else str(transcript_path.absolute()),
        transcript_path, raw, cwd, prompt[:500], origin_sources,
        logical_revision=logical_revision)
    return result.status == 'published'


def write_session(seen: dict, source: str, conv_id: str, messages: list, cwd: str = "", *, origin_sources=()) -> bool:
    """Write one conversation as a transcript and queue it, unless unchanged.

    `messages` are dicts: {"role": "user"|"assistant", "text": str,
    "tools": [{"name", "input", "output"}], "thinking": [str]}.
    """
    messages = [m for m in messages if (m.get("text") or "").strip() or m.get("tools")]
    prompts = [m["text"].strip() for m in messages if m["role"] == "user" and (m.get("text") or "").strip()]
    if not prompts or not any(m["role"] == "assistant" for m in messages):
        return False

    # Session state is shared by all assistants. Hash the complete source and
    # conversation ID, so equal IDs from different apps and IDs differing only
    # in punctuation cannot overwrite each other's transcripts or notes.
    conv_id = str(conv_id)
    session_id = hashlib.sha256(f"{source}:{conv_id}".encode()).hexdigest()[:32]
    digest = hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest()
    key = f"{source}:{session_id}"
    path = TRANSCRIPTS_DIR / source / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    origins = sorted({str(Path(item).absolute()) for item in origin_sources}) or ['unknown-import-origin']
    lines = []
    for n, m in enumerate(messages):
        metadata = {"uuid": m.get("id") or hashlib.sha256(f"{source}:{conv_id}:{n}".encode()).hexdigest()[:24],
                    "timestamp": m.get("timestamp", ""), "parentUuid": m.get("parent_id", ""),
                    "source": source, "source_id": conv_id, "source_revision": digest,
                    "derivation_sources": origins}
        if m["role"] == "user":
            lines.append({**metadata, "type": "user", "message": {"content": m.get("text", "")}})
            continue
        blocks = [{"type": "thinking", "thinking": t} for t in m.get("thinking") or [] if t]
        if (m.get("text") or "").strip():
            blocks.append({"type": "text", "text": m["text"]})
        results = []
        for i, tool in enumerate(m.get("tools") or []):
            tool_id = tool.get("id") or f"t{n}-{i}"
            tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {"input": tool.get("input")}
            blocks.append({"type": "tool_use", "id": tool_id, "name": tool.get("name") or "tool", "input": tool_input})
            if "output" in tool:
                results.append({**metadata, "type": "tool_result", "tool_use_id": tool_id,
                                "content": tool["output"], "is_error": tool.get("is_error", False),
                                "exit_code": tool.get("exit_code")})
        lines.append({**metadata, "type": "assistant", "message": {"content": blocks}})
        lines.extend(results)
    raw = "".join(json.dumps(line) + "\n" for line in lines).encode()
    # Every encounter can narrow controls, including an unchanged conversation
    # from a second export. Neither mutable JSONL nor seen is policy authority.
    from . import egress, import_publication
    egress.capture_restrictions(path, origins=origins, captured_text=raw.decode('utf-8'),
                                vault_root=TRANSCRIPTS_DIR)
    legacy_id = re.sub(r"[^A-Za-z0-9_-]", "", conv_id)[:80] or hashlib.sha256(conv_id.encode()).hexdigest()[:32]
    if seen.get(key) == digest or seen.get(f"{source}:{legacy_id}") == digest:
        import_publication.protect_existing(source, session_id, conv_id, raw, origins,
                                            logical_revision=digest)
        seen[key] = digest  # migrate the watermark without reimporting history
        return False
    from .state_io import atomic_bytes
    atomic_bytes(path, raw)
    queued = _enqueue(session_id, path, cwd, prompts[0], source, raw=raw,
                      logical_id=conv_id, logical_revision=digest, origin_sources=origins)
    seen[key] = digest
    return queued


# --------------------------------------------------------------------------
# Export zips (Claude.ai and ChatGPT "Export data")
# --------------------------------------------------------------------------

def _claude_export_messages(conv: dict) -> list:
    out = []
    for msg in conv.get("chat_messages") or []:
        role = "user" if msg.get("sender") == "human" else "assistant"
        texts = [b.get("text", "") for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"]
        thinking = [b.get("thinking", "") for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "thinking"]
        tools = [{"id": b.get("id"), "name": b.get("name"), "input": b.get("input")} for b in msg.get("content") or []
                 if isinstance(b, dict) and b.get("type") == "tool_use"]
        text = "\n\n".join(t for t in texts if t) or msg.get("text") or ""
        out.append({"role": role, "text": text, "tools": tools, "thinking": thinking,
                    "id": msg.get("uuid") or msg.get("id"), "timestamp": msg.get("created_at", ""),
                    "parent_id": msg.get("parent_uuid", "")})
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
            out.append({"role": role, "text": text, "id": msg.get("id") or node.get("id"),
                        "timestamp": msg.get("create_time", ""), "parent_id": node.get("parent", "")})
    return out


def _export_conversation(conv):
    """Validate required structure before deriving one independent conversation."""
    if not isinstance(conv, dict):
        raise ValueError('Export conversation is malformed')
    if 'chat_messages' in conv:
        if not isinstance(conv['chat_messages'], list):
            raise ValueError('Export conversation is malformed')
        source, conv_id, cwd = 'claude-ai', conv.get('uuid'), 'claude.ai'
        messages = _claude_export_messages(conv)
    elif 'mapping' in conv:
        mapping, node_id = conv['mapping'], conv.get('current_node')
        if not isinstance(mapping, dict) or not isinstance(node_id, str) or node_id not in mapping:
            raise ValueError('Export conversation is malformed')
        visited = set()
        while node_id:
            if node_id in visited or node_id not in mapping or len(visited) >= 100000:
                raise ValueError('Export parent chain is incomplete or cyclic')
            visited.add(node_id)
            node = mapping[node_id]
            if not isinstance(node, dict):
                raise ValueError('Export conversation is malformed')
            node_id = node.get('parent')
            if node_id is not None and not isinstance(node_id, str):
                raise ValueError('Export conversation is malformed')
        source, conv_id, cwd = 'chatgpt', conv.get('conversation_id') or conv.get('id'), 'chatgpt.com'
        messages = _chatgpt_export_messages(conv)
    else:
        raise ValueError('Export conversation is malformed')
    if not isinstance(conv_id, str) or not conv_id:
        raise ValueError('Export conversation identity is missing or malformed')
    return source, conv_id, messages, cwd


def _interactive(messages):
    return (any(message['role'] == 'user' and (message.get('text') or '').strip() for message in messages)
            and any(message['role'] == 'assistant' and ((message.get('text') or '').strip() or message.get('tools'))
                    for message in messages))


def import_zip(zip_path: Path, seen: dict, *, problems=None, report=None) -> Optional[int]:
    """None means readable non-export; recognized failures remain visible."""
    zip_path = Path(zip_path)
    queued, errors = 0, ImportProblems()
    local = {} if report is None else report
    _counter(local, 'archives_examined')
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [item for item in zf.infolist()
                       if re.fullmatch(r"(?:[^/]+/)?conversations(?:-\d+)?\.json", item.filename)]
            if not members:
                _counter(local, 'skipped_unrelated_archives')
                return None
            _counter(local, 'recognized_archives')
            for member_number, member in enumerate(members, 1):
                _counter(local, 'members_examined')
                label = f'{zip_path}#member-{member_number}'
                try:
                    if member.file_size > MAX_IMPORT_PAYLOAD_BYTES:
                        raise ValueError('Export member exceeds 256 MiB; preserve the original for lossless recovery')
                    with zf.open(member) as stream:
                        body = stream.read(MAX_IMPORT_PAYLOAD_BYTES + 1)
                    if len(body) > MAX_IMPORT_PAYLOAD_BYTES:
                        raise ValueError('Export member exceeds 256 MiB; preserve the original for lossless recovery')
                    convs = json.loads(body)
                    if not isinstance(convs, list):
                        raise ValueError('Export member must contain a conversation list')
                except Exception as error:
                    errors.append(_source_problem('zips', label, error))
                    _counter(local, 'member_failures')
                    continue
                for conversation_number, conv in enumerate(convs, 1):
                    _counter(local, 'conversations_examined')
                    try:
                        source, conv_id, messages, cwd = _export_conversation(conv)
                        if not _interactive(messages):
                            _counter(local, 'skipped_noninteractive_conversations')
                            continue
                        _counter(local, 'valid_interactive_conversations')
                        if write_session(seen, source, conv_id, messages, cwd, origin_sources=[zip_path]):
                            queued += 1
                        else:
                            _counter(local, 'unchanged_conversations')
                    except Exception as error:
                        errors.append(_source_problem('zips', f'{label}/conversation-{conversation_number}', error))
                        _counter(local, 'conversation_failures')
                        continue
    except Exception as error:
        errors.append(_source_problem('zips', zip_path, error))
        _counter(local, 'archive_failures')
    return _finish_source('zips', queued, errors, problems, local)


def sweep_zips(seen: dict, problems=None, *, report=None) -> int:
    """Import export zips; retain originals and archive newly imported versions."""
    done_dir = IMPORTS_DIR / "done"
    queued, errors = 0, ImportProblems()
    local = {} if report is None else report
    for folder in WATCH_DIRS:
        try:
            candidates = sorted(folder / n for n in os.listdir(folder) if n.endswith(".zip"))
        except FileNotFoundError:
            _counter(local, 'skipped_missing_watch_directories')
            continue
        except OSError as error:
            errors.append(_source_problem('zips', folder, error))
            _counter(local, 'directory_failures')
            continue
        for zip_path in candidates:
            if folder != IMPORTS_DIR and not _looks_like_export(zip_path):
                _counter(local, 'skipped_unrecognized_download_names')
                continue
            try:
                if time.time() - zip_path.stat().st_mtime < 60:
                    _counter(local, 'deferred_downloading_archives')
                    continue
                archive = {}
                count = import_zip(zip_path, seen, problems=errors, report=archive)
                for key, value in archive.items():
                    if key not in {'queued_sessions', 'failed_items', 'problem_details_omitted'}:
                        _counter(local, key, value)
                queued += count or 0
                if count is None or not archive.get('valid_interactive_conversations'):
                    continue
                try:
                    _archive_backup(zip_path, done_dir, local)
                except Exception as error:
                    errors.append(_source_problem('zips-backup', zip_path, error))
                    _counter(local, 'archive_backup_failures')
            except Exception as error:
                errors.append(_source_problem('zips', zip_path, error))
                _counter(local, 'archive_failures')
    return _finish_source('zips', queued, errors, problems, local)


def _archive_backup(zip_path, done_dir, report):
    """Retry an optional backup even when conversation publication is unchanged."""
    info = zip_path.stat()
    done_dir.mkdir(parents=True, exist_ok=True)
    target = done_dir / zip_path.name
    if target.exists():
        previous = target.stat()
        if (previous.st_size, previous.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
            _counter(report, 'archive_backups_present')
            return
        target = done_dir / f'{zip_path.stem}-{info.st_mtime_ns}-{info.st_size}.zip'
        if target.exists() and (target.stat().st_size, target.stat().st_mtime_ns) == (info.st_size, info.st_mtime_ns):
            _counter(report, 'archive_backups_present')
            return
    # Keep the original inspectable policy identity. Backup status says nothing
    # about failures in other members/conversations or complete import coverage.
    shutil.copy2(zip_path, target)
    _counter(report, 'archive_backups_copied')


def _looks_like_export(zip_path: Path) -> bool:
    """Claude: data-*.zip; ChatGPT: <64 hex>-<date>.zip. Content is checked after."""
    name = zip_path.name
    return name.startswith("data-") or bool(re.match(r"[0-9a-f]{64}-\d{4}-\d{2}-\d{2}", name))


# --------------------------------------------------------------------------
# Cursor
# --------------------------------------------------------------------------

def _cursor_workspace_folders(*, problems=None, report=None) -> dict:
    """composerId -> workspace folder, from each workspace's own state.vscdb."""
    folders, errors = {}, ImportProblems()
    for ws in CURSOR_WORKSPACES.glob("*"):
        _counter(report, 'workspace_sources_examined')
        db = None
        try:
            if not (ws / 'workspace.json').exists():
                _counter(report, 'skipped_workspaces_without_metadata')
                continue
            with (ws / 'workspace.json').open('rb') as stream:
                raw = stream.read(MAX_IMPORT_PAYLOAD_BYTES + 1)
            if len(raw) > MAX_IMPORT_PAYLOAD_BYTES:
                raise ValueError('Cursor source exceeds 256 MiB')
            metadata = json.loads(raw)
            folder = metadata.get('folder', '') if isinstance(metadata, dict) else None
            if not isinstance(folder, str):
                raise ValueError('Cursor workspace metadata is malformed')
            if not folder:
                _counter(report, 'skipped_workspaces_without_folder')
                continue
            db = sqlite3.connect((ws / 'state.vscdb').absolute().as_uri() + '?mode=ro', uri=True, timeout=5)
            db.execute('PRAGMA query_only=ON')
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_IMPORT_PAYLOAD_BYTES + 65536)
            row = db.execute("select value from ItemTable where key='composer.composerData'").fetchone()
            if row and row[0] is not None and (not isinstance(row[0], (str, bytes)) or
                    len(row[0].encode() if isinstance(row[0], str) else row[0]) > MAX_IMPORT_PAYLOAD_BYTES):
                raise ValueError('Cursor source exceeds 256 MiB')
            data = json.loads(row[0]) if row and row[0] else {}
            composers = data.get('allComposers', []) if isinstance(data, dict) else None
            if not isinstance(composers, list):
                raise ValueError('Cursor workspace metadata is malformed')
            for comp in composers:
                if not isinstance(comp, dict) or (comp.get('composerId') is not None and not isinstance(comp['composerId'], str)):
                    raise ValueError('Cursor workspace metadata is malformed')
                if comp.get('composerId'):
                    folders[comp['composerId']] = folder.removeprefix('file://')
        except Exception as error:
            errors.append(_source_problem('cursor-workspace', ws, error))
            _counter(report, 'workspace_source_failures')
            continue
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception as error:
                    errors.append(_source_problem('cursor-workspace', ws, error))
                    _counter(report, 'workspace_source_failures')
    _finish_source('cursor-workspaces', 0, errors, problems)
    return folders


def _cursor_bubble_message(bubble: dict) -> Optional[dict]:
    if not isinstance(bubble, dict):
        raise ValueError('Cursor required bubble is missing or malformed')
    role = {1: "user", 2: "assistant"}.get(bubble.get("type"))
    if not role:
        raise ValueError('Cursor bubble type is unsupported')
    if bubble.get('text') is not None and not isinstance(bubble['text'], str):
        raise ValueError('Cursor required bubble is missing or malformed')
    tools = []
    tf = bubble.get("toolFormerData")
    tf = {} if tf is None else tf
    if not isinstance(tf, dict):
        raise ValueError('Cursor required bubble is missing or malformed')
    if tf and not tf.get('name'):
        raise ValueError('Cursor required bubble is missing or malformed')
    if tf.get("name"):
        if not isinstance(tf['name'], str):
            raise ValueError('Cursor required bubble is missing or malformed')
        raw_args = tf.get('params', tf.get('rawArgs', '{}'))
        try:
            params = json.loads(raw_args) if isinstance(raw_args, (str, bytes)) else raw_args
        except ValueError:
            params = {"input": raw_args}
        params = params if isinstance(params, dict) else {"input": params}
        path = params.get("relativeWorkspacePath") or params.get("targetFile")
        if path and "file_path" not in params:
            params["file_path"] = path
        tool = {"name": tf["name"], "input": params}
        if 'result' in tf:
            tool['output'] = tf['result']  # full normalized evidence, never a 2,000-character view
        tools.append(tool)
    thinking_data = bubble.get('thinking')
    if thinking_data is not None and not isinstance(thinking_data, dict):
        raise ValueError('Cursor required bubble is missing or malformed')
    thinking = (thinking_data or {}).get('text')
    if thinking is not None and not isinstance(thinking, str):
        raise ValueError('Cursor required bubble is missing or malformed')
    return {"role": role, "text": bubble.get("text") or "", "tools": tools, "thinking": [thinking] if thinking else []}


def sweep_cursor(seen: dict, since_ts: float, db_path: Path = None, *, problems=None, report=None) -> int:
    db_path = Path(db_path or CURSOR_DB)
    queued, errors = 0, ImportProblems()
    local = {} if report is None else report
    if not db_path.exists():
        _counter(local, 'skipped_missing_optional_databases')
        return _finish_source('cursor', 0, errors, problems, local)
    db = None
    try:
        db = sqlite3.connect(db_path.absolute().as_uri() + '?mode=ro', uri=True, timeout=10)
        db.execute('PRAGMA query_only=ON')
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_IMPORT_PAYLOAD_BYTES + 65536)
        folders = None
        _counter(local, 'database_scans_started')
        # Fetch keys independently: a bad composer/bubble cannot abort the key
        # scan or cause an otherwise complete conversation to be omitted.
        for (key,) in db.execute("select key from cursorDiskKV where key like 'composerData:%'"):
            _counter(local, 'composers_examined')
            label = f'{db_path}#composer-{hashlib.sha256(str(key).encode()).hexdigest()[:16]}'
            try:
                row = db.execute('select value from cursorDiskKV where key=?', (key,)).fetchone()
                value = row[0] if row else None
                if not isinstance(value, (str, bytes)) or len(value.encode() if isinstance(value, str) else value) > MAX_IMPORT_PAYLOAD_BYTES:
                    raise ValueError('Cursor source exceeds 256 MiB')
                comp = json.loads(value) if value else None
                if not isinstance(comp, dict):
                    raise ValueError('Cursor composer is malformed')
                if 'conversation' not in comp and 'fullConversationHeadersOnly' not in comp:
                    raise ValueError('Cursor conversation is missing or malformed')
                updated = comp.get('lastUpdatedAt') or comp.get('createdAt') or 0
                if type(updated) not in (int, float) or not math.isfinite(updated):
                    raise ValueError('Cursor composer is malformed')
                if updated / 1000 < since_ts:
                    _counter(local, 'skipped_old_composers')
                    continue
                comp_id = comp.get('composerId') or key.split(':', 1)[1]
                if not isinstance(comp_id, str) or not comp_id:
                    raise ValueError('Cursor composer is malformed')
                if comp.get('conversation'):
                    bubbles = comp['conversation']
                    if not isinstance(bubbles, list):
                        raise ValueError('Cursor conversation is missing or malformed')
                else:
                    headers = comp.get('fullConversationHeadersOnly')
                    if headers is None and comp.get('conversation') == []:
                        headers = []
                    if not isinstance(headers, list):
                        raise ValueError('Cursor conversation is missing or malformed')
                    bubbles, payload_bytes = [], len(value.encode() if isinstance(value, str) else value)
                    for header in headers:
                        if not isinstance(header, dict) or not isinstance(header.get('bubbleId'), str) or not header['bubbleId']:
                            raise ValueError('Cursor required bubble is missing or malformed')
                        row = db.execute('select value from cursorDiskKV where key=?',
                            (f"bubbleId:{comp_id}:{header['bubbleId']}",)).fetchone()
                        if not row or not isinstance(row[0], (str, bytes)) or not row[0]:
                            raise ValueError('Cursor required bubble is missing or malformed')
                        raw = row[0].encode() if isinstance(row[0], str) else row[0]
                        payload_bytes += len(raw)
                        if payload_bytes > MAX_IMPORT_PAYLOAD_BYTES:
                            raise ValueError('Cursor source exceeds 256 MiB')
                        bubbles.append(json.loads(raw))
                messages = [_cursor_bubble_message(bubble) for bubble in bubbles]
                if not _interactive(messages):
                    _counter(local, 'skipped_noninteractive_composers')
                    continue
                if folders is None:
                    folders = _cursor_workspace_folders(problems=errors, report=local)
                if write_session(seen, 'cursor', comp_id, messages, folders.get(comp_id) or 'cursor', origin_sources=[db_path]):
                    queued += 1
                else:
                    _counter(local, 'unchanged_composers')
            except Exception as error:
                errors.append(_source_problem('cursor', label, error))
                _counter(local, 'composer_failures')
                continue
        _counter(local, 'database_scans_completed')
    except Exception as error:
        errors.append(_source_problem('cursor-database', db_path, error))
        _counter(local, 'database_scan_failures')
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as error:
                errors.append(_source_problem('cursor-database', db_path, error))
                _counter(local, 'database_close_failures')
    return _finish_source('cursor', queued, errors, problems, local)


# --------------------------------------------------------------------------
# Codex rollouts not delivered by the hook
# --------------------------------------------------------------------------

def sweep_codex(seen: dict, since_ts: float, sessions_dir: Path = None, *, problems=None) -> int:
    from .transcript_reader import check_transcript_size, codex_session_meta, read_transcript
    from . import session_tracker, import_publication

    sessions_dir = sessions_dir or CODEX_SESSIONS
    queued, errors = 0, ImportProblems()
    now = time.time()
    for path in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError as error:
            errors.append(_source_problem('codex', path, error))
            continue
        if mtime < since_ts or now - mtime < CODEX_IDLE_SECONDS:
            continue
        key = f"codex:{path.name}"
        if seen.get(key) == mtime:
            continue
        try:
            check_transcript_size(path)
            meta = codex_session_meta(path, strict=True)
            session_id = meta.get("id")
            if meta.get("background"):
                seen[key] = mtime
                continue
            if (key not in seen and session_tracker.get_state_file(session_id).exists()
                    and not import_publication.has_history('codex', session_id)):
                seen[key] = mtime  # the hook already delivered this session
                continue
            content = read_transcript(path)
            _validate_native_content(content)
            if meta != codex_session_meta(path, content._source_bytes, strict=True):
                raise ValueError('Native source metadata changed before capture')
            if content.user_prompts and content.assistant_texts:
                queued += _enqueue(session_id, path, meta.get("cwd", ""), content.user_prompts[0], "codex",
                                   raw=content._source_bytes, origin_sources=[str(path.absolute())])
            seen[key] = mtime
        except Exception as error:
            errors.append(_source_problem('codex', path, error))
    return _finish_source('codex', queued, errors, problems)


def _claude_code_meta(path: Path, raw=None) -> dict:
    """sessionId, cwd and entrypoint from the first lines of a Claude Code transcript."""
    meta = {}
    from .transcript_reader import metadata_lines
    for line in metadata_lines(path, max_lines=51, raw=raw):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        for src, dst in (("sessionId", "id"), ("cwd", "cwd"), ("entrypoint", "entrypoint")):
            if row.get(src) and dst not in meta:
                if not isinstance(row[src], str):
                    raise ValueError('Claude Code session metadata is malformed')
                meta[dst] = row[src]
        if len(meta) == 3:
            break
    return meta


def sweep_claude_code(seen: dict, since_ts: float, projects_dir: Path = None,
                      limit: int = CLAUDE_CODE_MAX_PER_SWEEP, *, problems=None) -> int:
    """Queue interactive Claude Code sessions the hook did not deliver.

    Scripted runs (`claude -p`, the SDK: entrypoint `sdk-*`) are skipped: they
    are automation, including claude-note's own synthesis and the Sweat app's
    Ask, and on a busy Mac they outnumber real sessions 50 to 1. Subagent
    transcripts live one level deeper and are not globbed. Newest first, at most
    `limit` per sweep; the rest stay unseen for the next sweep.
    """
    from .transcript_reader import check_transcript_size, read_transcript
    from . import session_tracker, import_publication

    projects_dir = projects_dir or CLAUDE_PROJECTS
    now = time.time()
    candidates, errors = [], ImportProblems()
    for path in projects_dir.glob("*/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError as error:
            errors.append(_source_problem('claude-code', path, error))
            continue
        if mtime < since_ts or now - mtime < CODEX_IDLE_SECONDS:
            continue
        if seen.get(f"claude-code:{path.stem}") == mtime:
            continue
        candidates.append((mtime, path))
    queued = 0
    for mtime, path in sorted(candidates, reverse=True):
        if queued >= limit:
            break
        key = f"claude-code:{path.stem}"
        try:
            check_transcript_size(path)
            meta = _claude_code_meta(path)
            session_id = meta.get("id") or path.stem
            if str(meta.get("entrypoint", "")).startswith("sdk"):
                seen[key] = mtime
                continue
            if (key not in seen and session_tracker.get_state_file(session_id).exists()
                    and not import_publication.has_history('claude-code', session_id)):
                seen[key] = mtime  # the hook already delivered this session
                continue
            content = read_transcript(path)
            _validate_native_content(content)
            if meta != _claude_code_meta(path, content._source_bytes):
                raise ValueError('Native source metadata changed before capture')
            if content.user_prompts and content.assistant_texts:
                queued += _enqueue(session_id, path, meta.get("cwd", ""), content.user_prompts[0], "claude-code",
                                   raw=content._source_bytes, origin_sources=[str(path.absolute())])
            seen[key] = mtime
        except Exception as error:
            errors.append(_source_problem('claude-code', path, error))
    return _finish_source('claude-code', queued, errors, problems)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def sweep(since: Optional[datetime] = None, zips: Iterable[Path] = ()) -> dict:
    """Serialize worker and manual imports to preserve the seen-store and queue."""
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _sweep(since=since, zips=zips)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _sweep(since: Optional[datetime] = None, zips: Iterable[Path] = ()) -> dict:
    """Run every source once; touch the heartbeat on success."""
    seen = _load_seen()
    since_ts = _since(seen, since)
    counts, problems = {}, ImportProblems()
    reports = {'zips': {}, 'cursor': {}}
    for name, fn in (("zips", lambda: sweep_zips(seen, problems, report=reports['zips'])),
                     ("cursor", lambda: sweep_cursor(seen, since_ts, problems=problems, report=reports['cursor'])),
                     ("codex", lambda: sweep_codex(seen, since_ts, problems=problems)),
                     ("claude-code", lambda: sweep_claude_code(seen, since_ts, problems=problems))):
        try:
            counts[name] = fn()
        except SourceImportError as error:
            counts[name] = error.queued
            failures = ImportProblems()
            failures.extend(error.problems)
            failures.total = error.failed
            problems.extend(failures)
        except Exception as error:  # one broken source must not stop the others
            counts[name] = 0
            problems.append(_source_problem(name, name, error))
        _save_seen(seen)
    for zip_path in zips:
        reports[str(zip_path)] = {}
        count = import_zip(Path(zip_path), seen, problems=problems, report=reports[str(zip_path)])
        counts[str(zip_path)] = count or 0
        _save_seen(seen)
    if problems:
        counts["problems"] = problems
        counts["failed"] = problems.total
        counts['problem_details_omitted'] = problems.omitted
    counts['reports'] = reports
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    # Keep the latest attempt separate from the last successful sweep. Health
    # checks can show a source error immediately without calling it a success.
    status = HEARTBEAT.with_suffix(".json")
    tmp = status.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": datetime.now().isoformat(), "counts": counts}))
    tmp.replace(status)
    # Touched on every clean sweep, including one that found nothing; a source
    # that raised leaves it stale so the owner's app notices within the hour.
    if problems:
        return counts
    HEARTBEAT.write_text(json.dumps({"ts": datetime.now().isoformat(), "counts": counts}))
    return counts
