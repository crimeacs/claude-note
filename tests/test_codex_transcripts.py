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

    def test_claude_tool_results_in_user_content_preserve_failures(self):
        path = _write([
            {"type": "user", "message": {"content": "Run the check"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "check"}},
                {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/repo/a.py"}},
            ]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                 "content": [{"type": "text", "text": "check failed"}]},
                {"type": "tool_result", "tool_use_id": "t2", "content": "line 1\nline 2"},
            ]}},
        ])
        content = read_transcript(path)
        self.assertEqual(content.user_prompts, ["Run the check"])
        self.assertEqual(content.errors, ["Bash: check failed"])
        self.assertFalse(content.tool_uses[0].success)
        self.assertEqual(content.tool_uses[0].output_summary, "check failed")
        self.assertEqual(content.tool_uses[1].output_summary, "(2 lines)")

    def test_codex_xml_request_is_kept_and_fork_metadata_does_not_change_identity(self):
        path = _write([
            {"type": "session_meta", "payload": {"id": "child-session"}},
            {"type": "session_meta", "payload": {"id": "parent-session"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>injected</environment_context>"},
                {"type": "input_text", "text": "<external_codex_apps_open_page>{}</external_codex_apps_open_page>"},
                {"type": "input_text", "text": "<article>Rewrite this paragraph</article>"},
            ]}},
        ])
        content = read_transcript(path)
        self.assertEqual(content.session_id, "child-session")
        self.assertEqual(content.user_prompts, ["<article>Rewrite this paragraph</article>"])

    def test_malformed_records_do_not_hide_the_rest_of_the_rollout(self):
        path = _write([
            None,
            {"type": "session_meta", "payload": {"id": "s1"}},
            17,
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                    "content": [{"type": "output_text", "text": "Answer"}]}},
        ])
        self.assertEqual(read_transcript(path).assistant_texts, ["Answer"])


if __name__ == "__main__":
    unittest.main()
