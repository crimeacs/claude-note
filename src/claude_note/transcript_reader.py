"""
Transcript reader for claude-note synthesizer.

Parses Claude Code transcript JSONL and extracts content for synthesis.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


@dataclass
class ToolUse:
    """A single tool invocation."""
    name: str
    input: dict
    output_summary: Optional[str] = None
    success: bool = True


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
                }
                for t in self.tool_uses
            ],
            "files_touched": self.files_touched,
            "errors": self.errors,
            "thinking_snippets": self.thinking_snippets,
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


def _summarize_tool_output(tool_name: str, output: str, max_len: int = 200) -> str:
    """Create a brief summary of tool output."""
    if not output:
        return ""

    # For file reads, just note the length
    if tool_name == "Read":
        lines = output.count("\n") + 1
        return f"({lines} lines)"

    # For search tools, count matches
    if tool_name in ("Glob", "Grep"):
        matches = output.count("\n") + 1 if output.strip() else 0
        return f"({matches} matches)"

    # For bash, truncate output
    if tool_name == "Bash":
        if len(output) > max_len:
            return output[:max_len] + "..."
        return output

    # Default: truncate
    if len(output) > max_len:
        return output[:max_len] + "..."
    return output


# Codex injects context as user messages. Only discard known context wrappers:
# a real request may itself begin with XML or HTML.
_CODEX_INJECTED_PREFIXES = (
    "<environment_context>", "<external_codex_apps_open_page>",
    "<codex_internal_context", "<heartbeat>",
    "# AGENTS.md instructions", "# Files mentioned by the user",
)


_CODEX_PATCH_FILE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: ([^\n\\\"'`]+)")


def _is_codex_transcript(transcript_path: Path) -> bool:
    """Codex rollouts are JSONL whose records carry a `payload` object."""
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
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


def codex_session_meta(transcript_path) -> dict:
    """First-record metadata of a Codex rollout: id, cwd, and whether it is a
    background run (a subagent thread, or `codex exec` automation) that should
    not become a note of its own. Empty dict if not a Codex rollout."""
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            entry = json.loads(f.readline())
    except (OSError, ValueError):
        return {}
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        return {}
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return {}
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
        return _tool_output_text(output.get("content", output.get("output", "")))
    return ""


def _apply_tool_result(content: TranscriptContent, calls: dict, tool_id, output,
                       is_error: bool = False) -> None:
    tool = calls.get(tool_id)
    if tool is None:
        return
    text = _tool_output_text(output)
    if is_error:
        tool.success = False
        if text:
            content.errors.append(f"{tool.name}: {text[:200]}")
    if text:
        tool.output_summary = _summarize_tool_output(tool.name, text)


def _read_codex_transcript(transcript_path: Path) -> TranscriptContent:
    """Parse a Codex rollout (`~/.codex/sessions/**/rollout-*.jsonl`)."""
    meta = codex_session_meta(transcript_path)
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

    with open(transcript_path, encoding="utf-8", errors="replace") as f:
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
                    if not text.startswith(_CODEX_INJECTED_PREFIXES):
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
                tool_use = ToolUse(name=name, input=tool_input)
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


def read_transcript(transcript_path: Union[str, Path]) -> TranscriptContent:
    """
    Read and parse a transcript JSONL file: Claude Code's format, or a Codex
    rollout (Codex CLI and the ChatGPT desktop app).

    Args:
        transcript_path: Path to the transcript JSONL file

    Returns:
        TranscriptContent with extracted data
    """
    transcript_path = Path(transcript_path)

    if not transcript_path.exists():
        raise FileNotFoundError(f"Transcript not found: {transcript_path}")

    if _is_codex_transcript(transcript_path):
        return _read_codex_transcript(transcript_path)

    # Extract session_id from path (last component before .jsonl)
    session_id = transcript_path.stem

    content = TranscriptContent(session_id=session_id)
    files_seen = set()
    current_tool_uses = {}  # Track tool uses by id for matching with results

    with open(transcript_path, encoding="utf-8", errors="replace") as f:
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

                _apply_tool_result(content, current_tool_uses, tool_use_id, content_data, is_error)

    return content


def read_transcript_from_state(state) -> TranscriptContent:
    """
    Read transcript for a session from its state.

    Args:
        state: SessionState object

    Returns:
        TranscriptContent with extracted data
    """
    if not state.transcript_path:
        raise ValueError("Session state has no transcript_path")

    return read_transcript(state.transcript_path)


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
