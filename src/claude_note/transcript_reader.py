"""
Transcript reader for claude-note synthesizer.

Parses Claude Code transcript JSONL and extracts content for synthesis.
"""

import json
import hashlib
import io
import os
import re
import stat
import tempfile
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Union

MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
MAX_NATIVE_RECORD_BYTES = 64 * 1024 * 1024
MAX_NATIVE_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
TRANSCRIPT_LIMIT_ERROR = "Transcript exceeds 256 MB; preserve the original for explicit lossless recovery"
METADATA_LIMIT_ERROR = "Transcript metadata exceeds 1 MB"


def check_transcript_size(path):
    """Reject known oversized/nonregular sources before reading metadata."""
    info = Path(path).stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Transcript source must be a regular file")
    if info.st_size > MAX_TRANSCRIPT_BYTES:
        raise ValueError(TRANSCRIPT_LIMIT_ERROR)


_NATIVE_IDENTITY_FIELDS = {
    "type", "uuid", "timestamp", "parentUuid", "sessionId", "cwd", "entrypoint",
    "source", "source_id", "source_revision", "ingested_at", "derivation_sources",
}
_CLAUDE_CONTENT_FIELDS = {"message", "result", "content", "tool_use_id", "is_error", "exit_code"}


def _native_record(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("Transcript contains malformed or incomplete JSONL records")
        record[key] = value
    return record


def _project_native_media(value):
    """Replace recognized binary media only; ordinary authored text stays exact."""
    if isinstance(value, list):
        return [_project_native_media(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected = {}
    for key, item in value.items():
        binary = (isinstance(item, str) and (
            (key == "data" and value.get("type") == "base64"
                and isinstance(value.get("media_type"), str)
                and value["media_type"].startswith(("image/", "audio/", "video/", "application/pdf")))
            or (key == "image_url" and item.startswith("data:image/"))
            or (key == "url" and value.get("type") in {"image", "input_image", "output_image"}
                and item.startswith("data:image/"))))
        if binary:
            digest = hashlib.sha256(item.encode("utf-8")).hexdigest()
            projected[key] = f"[binary media preserved in original record; sha256:{digest}; characters:{len(item)}]"
        else:
            projected[key] = _project_native_media(item)
    return projected


def capture_native_transcript(path):
    """Return a bounded text projection for an oversized native JSONL source.

    Small sources retain their byte-for-byte capture path (return None). Large
    sources keep all fields used by the reader, but binary media and duplicate
    host bookkeeping remain in the unmodified original. Each projected record
    is bound to its original line and digest; the manifest binds the full source.
    No malformed, incomplete, changing, or still-oversized projection is accepted.
    """
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Transcript source must be a regular file")
    if before.st_size <= MAX_TRANSCRIPT_BYTES:
        return None
    if before.st_size > MAX_NATIVE_SOURCE_BYTES:
        raise ValueError(TRANSCRIPT_LIMIT_ERROR)
    source_hash, source_bytes, records, projected_bytes = hashlib.sha256(), 0, 0, 0
    codex = None
    with path.open("rb") as source, tempfile.TemporaryFile() as projected:
        opened = source.fileno()
        opened_info = os.fstat(opened)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                opened_info.st_dev, opened_info.st_ino, opened_info.st_size, opened_info.st_mtime_ns, opened_info.st_ctime_ns):
            raise ValueError("Native source changed during capture")
        while True:
            line = source.readline(MAX_NATIVE_RECORD_BYTES + 1)
            if not line:
                break
            source_bytes += len(line)
            if len(line) > MAX_NATIVE_RECORD_BYTES or source_bytes > MAX_NATIVE_SOURCE_BYTES:
                raise ValueError(TRANSCRIPT_LIMIT_ERROR)
            if not line.endswith(b"\n"):
                raise ValueError("Transcript contains malformed or incomplete JSONL records")
            source_hash.update(line)
            records += 1
            try:
                row = json.loads(line.decode("utf-8", errors="strict"), object_pairs_hook=_native_record)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ValueError("Transcript contains malformed or incomplete JSONL records") from error
            if not isinstance(row, dict):
                raise ValueError("Transcript contains malformed or incomplete JSONL records")
            if codex is None:
                codex = isinstance(row.get("payload"), dict)
            kept = {key: item for key, item in row.items() if key in _NATIVE_IDENTITY_FIELDS}
            if codex:
                payload = row.get("payload")
                if row.get("type") in {"session_meta", "response_item"}:
                    kept["payload"] = payload
                elif isinstance(payload, dict):
                    kept["payload"] = {"type": payload.get("type", "")}
            elif row.get("type") in {"user", "assistant", "progress", "tool_result"}:
                kept.update({key: item for key, item in row.items() if key in _CLAUDE_CONTENT_FIELDS})
            kept = _project_native_media(kept)
            kept["_native_origin"] = {"line": records, "payload_digest": hashlib.sha256(line.rstrip(b"\r\n")).hexdigest()}
            encoded = json.dumps(kept, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            projected_bytes += len(encoded)
            if projected_bytes > MAX_TRANSCRIPT_BYTES:
                raise ValueError(TRANSCRIPT_LIMIT_ERROR)
            projected.write(encoded)
        after = os.fstat(opened)
        current = path.stat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(before) != identity(current) or source_bytes != before.st_size:
            raise ValueError("Native source changed during capture")
        manifest = {"version": 1, "source": str(path.absolute()), "revision": source_hash.hexdigest(),
                    "bytes": source_bytes, "records": records, "representation": "native-text-projection"}
        projected.seek(0)
        first = json.loads(projected.readline())
        first["_native_projection"] = manifest
        raw = json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" + projected.read()
        if len(raw) > MAX_TRANSCRIPT_BYTES:
            raise ValueError(TRANSCRIPT_LIMIT_ERROR)
        return raw


def metadata_lines(path, *, max_lines=1, raw=None):
    """Bound both individual JSONL records and the whole metadata prefix."""
    with (Path(path).open('rb') if raw is None else io.BytesIO(raw)) as source:
        remaining = MAX_METADATA_BYTES
        for _ in range(max_lines):
            line = source.readline(remaining + 1)
            if not line:
                break
            if len(line) > remaining:
                raise ValueError(METADATA_LIMIT_ERROR)
            remaining -= len(line)
            yield line


@dataclass
class ToolUse:
    """A single tool invocation."""
    name: str
    input: dict
    output_summary: Optional[str] = None
    success: Optional[bool] = None
    call_id: str = ""
    output: str = ""                 # lossless local evidence, not an implicit egress grant
    source_span: str = ""

    @property
    def outcome(self) -> str:
        return "unknown" if self.success is None else "success" if self.success else "failure"


@dataclass
class SourceEvent:
    """Immutable source anchor; the original JSONL remains the payload store."""
    event_id: str
    source: str
    revision: str
    line: int
    observed_at: str
    parent_id: str
    kind: str
    payload_digest: str
    origin_source: str = ""
    origin_revision: str = ""
    ingested_at: str = ""
    origin_line: int = 0
    origin_payload_digest: str = ""

    @property
    def anchor(self) -> str:
        return f"{self.source}#L{self.line}@{self.revision}"


@dataclass
class TranscriptContent:
    """Extracted content from a transcript."""
    session_id: str
    user_prompts: list[str] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)
    tool_uses: list[ToolUse] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    thinking_snippets: list[str] = field(default_factory=list)
    source_events: list[SourceEvent] = field(default_factory=list)
    source_revision: str = ""
    source_path: str = ""
    coverage: dict = field(default_factory=dict)
    context_sources: list[str] = field(default_factory=list)
    snapshot_path: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "session_id": self.session_id,
            "user_prompts": self.user_prompts,
            "assistant_texts": self.assistant_texts,
            "tool_uses": [
                {
                    "name": t.name,
                    "input": t.input,
                    "output_summary": t.output_summary,
                    "success": t.success,
                    "outcome": t.outcome,
                    "call_id": t.call_id,
                    "source_span": t.source_span,
                    "output": t.output,
                }
                for t in self.tool_uses
            ],
            "files_touched": self.files_touched,
            "errors": self.errors,
            "thinking_snippets": self.thinking_snippets,
            "source_events": [asdict(e) for e in self.source_events],
            "source_revision": self.source_revision,
            "source_path": self.source_path,
            "coverage": self.coverage,
            "snapshot_path": self.snapshot_path,
        }


def _extract_file_paths(tool_name: str, tool_input: dict) -> list[str]:
    """Extract file paths from tool input."""
    paths = []

    # Direct file_path parameter
    if "file_path" in tool_input:
        paths.append(tool_input["file_path"])

    # Bash commands that might touch files
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Simple heuristic: extract paths from common commands
        # This is intentionally conservative
        pass

    # Glob/Grep paths
    if tool_name in ("Glob", "Grep"):
        if "path" in tool_input:
            paths.append(tool_input["path"])

    return paths


def _summarize_tool_output(tool_name: str, output: str, max_len: int = 4000) -> str:
    """Bound the model view, keeping the payload available at its source anchor."""
    if not output:
        return ""

    if len(output) > max_len:
        half = max_len // 2
        return output[:half] + "\n[... view clipped; full output in source ...]\n" + output[-half:]
    return output


# Codex injects context as user messages. Only discard known context wrappers:
# a real request may itself begin with XML or HTML.
_CODEX_INJECTED_PREFIXES = (
    "<environment_context>", "<external_codex_apps_open_page>",
    "<codex_internal_context", "<heartbeat>",
    "# AGENTS.md instructions",
)


_CODEX_PATCH_FILE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"'`]+)")


def _stream(path, raw=None):
    return open(path, encoding="utf-8", errors="replace") if raw is None else io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace")


def _is_codex_transcript(transcript_path: Path, raw=None) -> bool:
    """Codex rollouts are JSONL whose records carry a `payload` object."""
    with _stream(transcript_path, raw) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            return isinstance(entry, dict) and isinstance(entry.get("payload"), dict)
    return False


def codex_session_meta(transcript_path, raw=None, *, strict=False) -> dict:
    """First-record metadata of a Codex rollout: id, cwd, and whether it is a
    background run (a subagent thread, or `codex exec` automation) that should
    not become a note of its own. Empty dict if not a Codex rollout."""
    try:
        entry = json.loads(next(metadata_lines(transcript_path, raw=raw), b''))
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        if strict:
            raise ValueError("Codex session metadata is missing or malformed")
        return {}
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        if strict:
            raise ValueError("Codex session metadata is missing or malformed")
        return {}
    if strict and (not isinstance(payload.get("id") or payload.get("session_id"), str)
                   or not (payload.get("id") or payload.get("session_id"))
                   or not isinstance(payload.get('cwd', ''), str)):
        raise ValueError("Codex session metadata is missing or malformed")
    source = payload.get("source")
    background = source == "exec" or (isinstance(source, dict) and "subagent" in source)
    return {"id": payload.get("id") or payload.get("session_id"), "cwd": payload.get("cwd", ""), "background": background}


def _codex_texts(content, kinds: tuple) -> list[str]:
    if isinstance(content, str):
        return [content]
    texts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") in kinds:
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return texts


def _tool_output_text(output) -> str:
    """Text from either a legacy string or current tool-result content blocks."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(block["text"] for block in output
                         if isinstance(block, dict) and isinstance(block.get("text"), str))
    if isinstance(output, dict):
        if "stdout" in output or "stderr" in output:
            return "\n".join(str(output[k]) for k in ("stdout", "stderr") if output.get(k))
        return _tool_output_text(output.get("content", output.get("output", "")))
    return ""


def _apply_tool_result(content: TranscriptContent, calls: dict, tool_id, output,
                       is_error: bool = False, source_span: str = "") -> None:
    tool = calls.get(tool_id)
    if tool is None:
        return
    text = _tool_output_text(output)
    tool.output = text
    tool.source_span = source_span or tool.source_span
    # Codex's shell wrapper carries the exit code in its output. Arbitrary
    # words such as "success" or "error" are not outcome evidence.
    exit_match = re.search(r"(?m)^Process exited with code (-?\d+)\s*$", text)
    code = output.get("exit_code") if isinstance(output, dict) else None
    if code is None and exit_match:
        code = int(exit_match[1])
    if is_error or (isinstance(code, int) and code != 0):
        tool.success = False
        if text:
            content.errors.append(f"{tool.name}: {text[:200]}")
    elif isinstance(code, int) or (isinstance(output, dict) and output.get("is_error") is False):
        tool.success = True
    if text:
        tool.output_summary = _summarize_tool_output(tool.name, text)


def _read_codex_transcript(transcript_path: Path, raw=None) -> TranscriptContent:
    """Parse a Codex rollout (`~/.codex/sessions/**/rollout-*.jsonl`)."""
    meta = codex_session_meta(transcript_path, raw)
    content = TranscriptContent(session_id=meta.get("id") or transcript_path.stem)
    files_seen = set()
    calls = {}

    def touch(tool_name: str, tool_input: dict) -> None:
        paths = list(_extract_file_paths(tool_name, tool_input))
        # apply_patch bodies arrive raw, or embedded in an `exec` script as a
        # string literal with escaped newlines; match the marker either way.
        patch = tool_input.get("input") if isinstance(tool_input.get("input"), str) else ""
        paths.extend(m.strip() for m in _CODEX_PATCH_FILE.findall(patch))
        for path in paths:
            if path and path not in files_seen:
                content.files_touched.append(path)
                files_seen.add(path)

    with _stream(transcript_path, raw) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = entry.get("type")
            ptype = payload.get("type")

            if kind == "session_meta":
                # Forked rollouts can include their parent's metadata in the
                # inherited history. The first record identifies this session.
                continue
            elif kind != "response_item":
                continue
            elif ptype == "message" and payload.get("role") == "user":
                for text in _codex_texts(payload.get("content"), ("input_text", "text")):
                    # Attachment/context wrappers and the actual request may
                    # arrive in one message. Keep the explicit request intact.
                    if "## My request:" in text:
                        text = text.split("## My request:", 1)[1].strip()
                    if text and not text.startswith(_CODEX_INJECTED_PREFIXES):
                        content.user_prompts.append(text)
            elif ptype == "message" and payload.get("role") == "assistant":
                content.assistant_texts.extend(_codex_texts(payload.get("content"), ("output_text", "text")))
            elif ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name", "unknown")
                if ptype == "function_call":
                    try:
                        tool_input = json.loads(payload.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        tool_input = {"arguments": payload.get("arguments", "")}
                    if not isinstance(tool_input, dict):
                        tool_input = {"arguments": tool_input}
                else:
                    tool_input = {"input": payload.get("input", "")}
                tool_use = ToolUse(name=name, input=tool_input, call_id=payload.get("call_id", ""))
                content.tool_uses.append(tool_use)
                if payload.get("call_id"):
                    calls[payload["call_id"]] = tool_use
                touch(name, tool_input)
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                tool_use = calls.get(payload.get("call_id"))
                output = payload.get("output", "")
                _apply_tool_result(content, calls, payload.get("call_id"), output,
                                   isinstance(output, dict) and bool(output.get("is_error")))
            elif ptype == "reasoning":
                for item in payload.get("summary") or []:
                    text = item.get("text", "") if isinstance(item, dict) else ""
                    if text.strip():
                        content.thinking_snippets.append(text.strip()[:500])

    return content


def read_transcript(transcript_path: Union[str, Path], *, captured_bytes=None) -> TranscriptContent:
    """
    Read and parse a transcript JSONL file: Claude Code's format, or a Codex
    rollout (Codex CLI and the ChatGPT desktop app).

    Args:
        transcript_path: Path to the transcript JSONL file

    Returns:
        TranscriptContent with extracted data
    """
    transcript_path = Path(transcript_path)

    if captured_bytes is None:
        if not transcript_path.exists():
            raise FileNotFoundError(f"Transcript not found: {transcript_path}")
        check_transcript_size(transcript_path)
        with transcript_path.open("rb") as source:
            captured = source.read(MAX_TRANSCRIPT_BYTES + 1)
    else:
        if not isinstance(captured_bytes, bytes):
            raise ValueError("Captured transcript must be immutable bytes")
        captured = captured_bytes
    if len(captured) > MAX_TRANSCRIPT_BYTES:
        raise ValueError(TRANSCRIPT_LIMIT_ERROR)
    complete = captured.rfind(b"\n") + 1
    raw = captured[:complete]
    trailing_bytes = len(captured) - complete
    if _is_codex_transcript(transcript_path, raw):
        content = _read_codex_transcript(transcript_path, raw)
        return _attach_sources(content, transcript_path, raw, trailing_bytes)

    # Extract session_id from path (last component before .jsonl)
    session_id = transcript_path.stem

    content = TranscriptContent(session_id=session_id)
    files_seen = set()
    current_tool_uses = {}  # Track tool uses by id for matching with results

    with _stream(transcript_path, raw) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue

            entry_type = entry.get("type")

            # Handle user messages
            if entry_type == "user":
                message = entry.get("message", {})
                msg_content = message.get("content", "")
                if isinstance(msg_content, str) and msg_content.strip():
                    content.user_prompts.append(msg_content.strip())
                elif isinstance(msg_content, list):
                    # Handle content blocks
                    for block in msg_content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    content.user_prompts.append(text.strip())
                            elif block.get("type") == "tool_result":
                                _apply_tool_result(content, current_tool_uses,
                                                   block.get("tool_use_id"), block.get("content"),
                                                   bool(block.get("is_error")))

            # Handle assistant messages
            elif entry_type == "assistant":
                message = entry.get("message", {})
                msg_content = message.get("content", [])

                if isinstance(msg_content, list):
                    for block in msg_content:
                        if not isinstance(block, dict):
                            continue

                        block_type = block.get("type")

                        # Text response
                        if block_type == "text":
                            text = block.get("text", "")
                            if text.strip():
                                content.assistant_texts.append(text.strip())

                        # Tool use
                        elif block_type == "tool_use":
                            tool_name = block.get("name", "unknown")
                            tool_input = block.get("input", {})
                            tool_id = block.get("id", "")

                            tool_use = ToolUse(
                                name=tool_name,
                                input=tool_input,
                                call_id=tool_id,
                            )
                            content.tool_uses.append(tool_use)

                            # Track for later result matching
                            if tool_id:
                                current_tool_uses[tool_id] = tool_use

                            # Extract file paths
                            for path in _extract_file_paths(tool_name, tool_input):
                                if path and path not in files_seen:
                                    content.files_touched.append(path)
                                    files_seen.add(path)

                        # Thinking blocks
                        elif block_type == "thinking":
                            thinking = block.get("thinking", "")
                            if thinking.strip():
                                # Only keep first 500 chars of each thinking block
                                snippet = thinking.strip()[:500]
                                content.thinking_snippets.append(snippet)

            # Handle tool results (progress messages)
            elif entry_type == "progress":
                # Progress messages can contain tool results
                tool_use_id = entry.get("tool_use_id")
                result = entry.get("result", {})

                if isinstance(result, dict):
                    _apply_tool_result(content, current_tool_uses, tool_use_id,
                                       result.get("content"), bool(result.get("is_error")))

            # Handle tool result messages
            elif entry_type == "tool_result":
                tool_use_id = entry.get("tool_use_id")
                content_data = entry.get("content", "")
                is_error = entry.get("is_error", False)

                _apply_tool_result(content, current_tool_uses, tool_use_id,
                                   {"content": content_data, "exit_code": entry.get("exit_code")}, is_error)

    return _attach_sources(content, transcript_path, raw, trailing_bytes)


def _attach_sources(content: TranscriptContent, path: Path, raw=None, trailing_bytes=0) -> TranscriptContent:
    """Bind parsed views to exact original records without inventing timestamps."""
    raw = path.read_bytes() if raw is None else raw
    content._source_bytes = raw  # one immutable snapshot for parsing/checkpoint identity
    content.source_revision = hashlib.sha256(raw).hexdigest()
    content.source_path = str(path.absolute())
    malformed = 0
    projection = {}
    calls = {tool.call_id: tool for tool in content.tool_uses if tool.call_id}
    for number, line in enumerate(raw.splitlines(), 1):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        if number == 1 and isinstance(record.get("_native_projection"), dict):
            candidate = record["_native_projection"]
            if (candidate.get("version") == 1 and candidate.get("source") == content.source_path
                    and isinstance(candidate.get("revision"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", candidate["revision"])
                    and candidate.get("representation") == "native-text-projection"):
                projection = candidate
        origins = record.get('derivation_sources')
        if isinstance(origins, list) and all(isinstance(item, str) for item in origins):
            content.context_sources.extend(item for item in origins if item not in content.context_sources)
        elif record.get('source') in {'claude-ai', 'chatgpt', 'cursor'}:
            content.context_sources.append('unknown-import-origin')
        payload = record.get("payload", record)
        if not isinstance(payload, dict):
            continue
        message = payload.get("message") or {}
        event_id = str(payload.get("id") or record.get("uuid") or message.get("id")
                       or hashlib.sha256(content.source_path.encode() + str(number).encode() + line).hexdigest()[:24])
        event = SourceEvent(event_id, content.source_path, content.source_revision, number,
                            str(record.get("timestamp") or payload.get("timestamp") or ""),
                            str(record.get("parentUuid") or payload.get("parent_id") or ""),
                            str(payload.get("type") or record.get("type") or "unknown"),
                            hashlib.sha256(line).hexdigest(),
                            str(record.get("source", "")) + (":" + str(record["source_id"]) if record.get("source_id") else ""),
                            str(record.get("source_revision", "")), str(record.get("ingested_at", "")))
        origin = record.get("_native_origin")
        if (projection and isinstance(origin, dict) and type(origin.get("line")) is int and origin["line"] > 0
                and isinstance(origin.get("payload_digest"), str)
                and re.fullmatch(r"[0-9a-f]{64}", origin["payload_digest"])):
            event.origin_source, event.origin_revision = projection["source"], projection["revision"]
            event.origin_line, event.origin_payload_digest = origin["line"], origin["payload_digest"]
        content.source_events.append(event)
        call_id = payload.get("call_id") or payload.get("tool_use_id")
        if call_id in calls:
            calls[call_id].source_span = event.anchor
        for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
            if isinstance(block, dict):
                call_id = block.get("tool_use_id") or block.get("id")
                if call_id in calls:
                    calls[call_id].source_span = event.anchor
    content.coverage = {"records": len(content.source_events), "malformed_records": malformed,
                        "payloads": "original-jsonl", "outcomes_unknown": sum(t.success is None for t in content.tool_uses),
                        "snapshot_bytes": len(raw), "snapshot_lines": len(raw.splitlines()), "snapshot_revision": content.source_revision}
    content.coverage["incomplete_trailing_bytes"] = trailing_bytes
    if projection:
        content.coverage["native_projection"] = projection
        content.coverage["payloads"] = "native-text-projection; binary and host bookkeeping in original source"
    return content


def derivation_sources(content, cwd=""):
    """Source policy follows evidence inputs, not just their transcript envelope."""
    paths = {content.source_path or "unknown-transcript", *content.context_sources}
    if cwd and Path(cwd).is_absolute():
        paths.add(str(Path(cwd).absolute()))
    for tool in content.tool_uses:
        for key in ("file_path", "path", "filename", "source_path"):
            value = tool.input.get(key) if isinstance(tool.input, dict) else None
            if isinstance(value, str) and value and not value.startswith(("http:", "https:")):
                paths.add(str(Path(cwd) / value) if cwd and not Path(value).is_absolute() else value)
    return sorted(paths)


def read_transcript_from_state(state, *, full=False) -> TranscriptContent:
    """
    Read transcript for a session from its state.

    Args:
        state: SessionState object

    Returns:
        TranscriptContent with extracted data
    """
    if not state.transcript_path:
        raise ValueError("Session state has no transcript_path")

    if getattr(state, "import_binding", {}):
        from . import import_publication
        publication, raw = import_publication.read_binding(state)
        content = read_transcript(state.transcript_path, captured_bytes=raw)
        content._captured_source = publication["snapshot_path"]
        content._captured_revision = publication["captured_revision"]
        content.context_sources.append(publication["snapshot_path"])
        content.coverage["import_publication"] = publication["publication_id"]
        content.coverage["import_logical_revision"] = publication["logical_revision"]
    else:
        content = read_transcript(state.transcript_path)
    checkpoint = {} if full else getattr(state, "transcript_checkpoint", {}) or {}
    size, start = checkpoint.get("bytes", 0), checkpoint.get("lines", 0)
    if not size or not start:
        return _snapshot(content, content._source_bytes)
    raw = content._source_bytes
    if size > len(raw) or hashlib.sha256(raw[:size]).hexdigest() != checkpoint.get("revision"):
        content.coverage["checkpoint"] = "source-changed-full-replay"
        return _snapshot(content, raw)
    # Parse locally once for relationship resolution, but extract only new
    # records. A result for a tool started before the checkpoint retains its
    # call input because matching occurred before this view was selected.
    content.source_events = [e for e in content.source_events if e.line > start]
    content.tool_uses = [t for t in content.tool_uses if t.source_span and
                        int(t.source_span.rsplit("#L", 1)[1].split("@", 1)[0]) > start]
    content.errors = [f"{t.name}: {t.output[:200]}" for t in content.tool_uses if t.success is False]
    content.user_prompts, content.assistant_texts, content.thinking_snippets = [], [], []
    for line in raw.splitlines()[start:]:
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload", entry)
        if not isinstance(payload, dict):
            continue
        message = payload.get("message") or payload
        role = message.get("role") or entry.get("type")
        texts = _codex_texts(message.get("content"), ("input_text", "output_text", "text"))
        for text in texts:
            if "## My request:" in text:
                text = text.split("## My request:", 1)[1].strip()
            if role == "user" and text and not text.startswith(_CODEX_INJECTED_PREFIXES):
                content.user_prompts.append(text)
            elif role == "assistant":
                content.assistant_texts.append(text)
    content.coverage.update({"checkpoint": "append-only-delta", "start_line": start + 1,
                             "base_revision": checkpoint["revision"], "records": len(content.source_events)})
    return _snapshot(content, raw[size:], start)


def _snapshot(content, raw, start=0):
    """Keep immutable appended evidence without copying every earlier turn."""
    from . import config, import_publication
    digest = hashlib.sha256(raw).hexdigest()
    segments = None
    captured = getattr(content, "_captured_source", "")
    if (captured and Path(captured).suffix == ".jsonl"
            and digest == getattr(content, "_captured_revision", "")):
        # Historical v1 evidence already has real full-file anchors. Reading
        # it is not permission to allocate or migrate another representation.
        snapshot = Path(captured)
    elif content.coverage.get("snapshot_bytes", len(raw)) >= import_publication.CHUNKED_SNAPSHOT_BYTES:
        from . import egress, source_store
        scope = import_publication.source_scope(content.source_path)
        snapshot = source_store.publish(raw, scope=scope)
        segments = source_store.segments(snapshot, digest, scope=scope)
        parent = captured
        if parent:
            # Reuse the import's frozen controls without reopening its mutable
            # original. Local evidence remains readable if that original is
            # later deleted; external delivery still checks its live lineage.
            protected_source = Path(parent)
            protected_revision = content._captured_revision
            if snapshot != protected_source:
                egress.record_derivation(snapshot, "---\nderivation_sources: " +
                    json.dumps([parent]) + "\n---\n", vault_root=config.STATE_DIR, budgeted=True)
        else:
            protected_source, protected_revision = snapshot, digest
            egress.capture_restrictions(snapshot,
                origins=list(dict.fromkeys([content.source_path, *content.context_sources])),
                captured_text=raw.decode("utf-8"), vault_root=config.STATE_DIR, budgeted=True)
        if not parent or snapshot != protected_source:
            try:
                egress.inherit_capture_restrictions(protected_source,
                    source_revision=protected_revision,
                    targets=list(dict.fromkeys((Path(part["path"]), part["revision"]) for part in segments)),
                    vault_root=config.STATE_DIR)
            except egress.UnresolvedSourceControls:
                pass  # Frozen unknown controls deny delivery, not local capture.
        if str(snapshot) not in content.context_sources:
            content.context_sources.append(str(snapshot))
        starts = [part["start_line"] for part in segments]
    else:
        snapshot = config.STATE_DIR / "sources" / (digest + ".jsonl")
        import_publication.store_small_snapshot(snapshot, raw)
    content.snapshot_path = str(snapshot.resolve())

    def locate(line):
        relative = line - start
        if segments is None:
            return content.snapshot_path, digest, relative
        index = bisect_right(starts, relative) - 1
        if index < 0 or relative >= starts[index] + segments[index]["lines"]:
            raise ValueError("Evidence line is outside its captured chunks")
        part = segments[index]
        return str(part["path"]), part["revision"], relative - part["start_line"] + 1

    for event in content.source_events:
        event.origin_source = event.origin_source or content.source_path
        event.origin_revision = event.origin_revision or content.source_revision
        event.origin_line = event.origin_line or event.line
        event.source, event.revision, event.line = locate(event.line)
    for tool in content.tool_uses:
        if tool.source_span:
            line = int(tool.source_span.rsplit("#L", 1)[1].split("@", 1)[0])
            path, revision, local_line = locate(line)
            tool.source_span = f"{path}#L{local_line}@{revision}"
    content.coverage["evidence_snapshot_revision"] = digest
    content.coverage["evidence_snapshot_bytes"] = len(raw)
    return content


def get_transcript_summary(content: TranscriptContent) -> dict:
    """
    Get a summary of transcript content for synthesis prompt.

    Returns a dict with counts and key snippets.
    """
    return {
        "num_user_prompts": len(content.user_prompts),
        "num_assistant_texts": len(content.assistant_texts),
        "num_tool_uses": len(content.tool_uses),
        "num_files_touched": len(content.files_touched),
        "num_errors": len(content.errors),
        "tool_breakdown": _count_tool_types(content.tool_uses),
    }


def _count_tool_types(tool_uses: list[ToolUse]) -> dict[str, int]:
    """Count tool uses by type."""
    counts = {}
    for tool in tool_uses:
        counts[tool.name] = counts.get(tool.name, 0) + 1
    return counts
