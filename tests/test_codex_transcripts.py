import json
import tempfile
import unittest
from pathlib import Path

from claude_note.transcript_reader import read_transcript


def _write(records):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for record in records:
        handle.write(json.dumps(record) + "\n")
    handle.close()
    return Path(handle.name)


class CodexTranscriptTests(unittest.TestCase):
    def test_reads_codex_rollout(self):
        path = _write([
            {"type": "session_meta", "payload": {"id": "sess-1", "cwd": "/repo"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
                {"type": "input_text", "text": "# AGENTS.md instructions for /repo"},
                {"type": "input_text", "text": "Make the ad music better"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Plan the mix"}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c1",
                "input": "*** Begin Patch\n*** Update File: /repo/music.py\n@@\n-a\n+b\n*** End Patch"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": "Done"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "c2",
                "arguments": "{\"cmd\": \"ls\"}"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Updated the mix."}]}},
            {"type": "event_msg", "payload": {"type": "token_count"}},
        ])
        content = read_transcript(path)
        self.assertEqual(content.session_id, "sess-1")
        self.assertEqual(content.user_prompts, ["Make the ad music better"])
        self.assertEqual(content.assistant_texts, ["Updated the mix."])
        self.assertEqual([t.name for t in content.tool_uses], ["apply_patch", "exec_command"])
        self.assertEqual(content.tool_uses[1].input, {"cmd": "ls"})
        self.assertIn("/repo/music.py", content.files_touched)
        self.assertEqual(content.thinking_snippets, ["Plan the mix"])

    def test_patch_inside_exec_script(self):
        script = 'await tools.apply_patch("*** Begin Patch\\n*** Add File: /repo/new.py\\n+x\\n*** End Patch")'
        path = _write([
            {"type": "session_meta", "payload": {"id": "s2"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": script}},
        ])
        self.assertEqual(read_transcript(path).files_touched, ["/repo/new.py"])

    def test_claude_transcripts_still_parse(self):
        path = _write([
            {"type": "user", "message": {"content": "hello"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        ])
        content = read_transcript(path)
        self.assertEqual(content.user_prompts, ["hello"])
        self.assertEqual(content.assistant_texts, ["hi"])


if __name__ == "__main__":
    unittest.main()
