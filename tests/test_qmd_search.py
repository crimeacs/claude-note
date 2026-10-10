"""Contract tests for current/legacy QMD output and source containment."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import config, qmd_search, synthesizer, transcript_reader, vault_indexer


class QMDTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(qmd_search, "_qmd_bin", return_value="/tools/qmd")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_current_array_and_legacy_object_keep_order(self):
        current = [{"file": "qmd://notes/first.md", "title": "First", "score": 0.05},
                   {"file": "qmd://notes/second.md", "score": 0.08, "snippet": "source"}]
        legacy = {"results": [{"path": "first.md", "title": "First", "score": "0.9"}]}
        for data, expected in ((current, ["qmd://notes/first.md", "qmd://notes/second.md"]),
                               (legacy, ["first.md"])):
            with mock.patch.object(qmd_search.subprocess, "run", return_value=mock.Mock(
                    returncode=0, stdout=json.dumps(data))) as run:
                results = qmd_search.search_keyword("retry", collection="notes", timeout=2)
            self.assertEqual([r.path for r in results], expected)
            self.assertEqual(run.call_args.args[0],
                             ["/tools/qmd", "search", "retry", "-n", "10", "--json", "-c", "notes"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            self.assertNotIn("--min-score", run.call_args.args[0])

    def test_bad_json_missing_binary_error_and_deadline_are_optional(self):
        for failure in (subprocess.TimeoutExpired("qmd", 1), OSError("unavailable")):
            with mock.patch.object(qmd_search.subprocess, "run", side_effect=failure):
                self.assertEqual(qmd_search.search_keyword("retry"), [])
        for output in ("not JSON", '{"results": 42}'):
            with mock.patch.object(qmd_search.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=output)):
                self.assertEqual(qmd_search.search_keyword("retry"), [])
        with mock.patch.object(qmd_search, "_qmd_bin", return_value=None), \
                mock.patch.object(qmd_search.subprocess, "run") as run:
            self.assertEqual(qmd_search.search_vector("retry"), [])
            run.assert_not_called()

    def test_vector_only_passes_similarity_threshold(self):
        with mock.patch.object(qmd_search.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="[]")) as run:
            qmd_search.search_vector("concept", limit=3, min_score=0.75, collection="notes")
        self.assertEqual(run.call_args.args[0][-2:], ["--min-score", "0.75"])

    def test_malformed_rows_do_not_poison_good_results(self):
        rows = [None, {"file": "bad.md", "score": "NaN"}, {"path": 42},
                {"file": "good.md", "score": 0.2, "snippet": {}},
                {"file": "invalid.md", "score": []}]
        result = qmd_search._parse_results(json.dumps(rows))
        self.assertEqual([(r.path, r.snippet) for r in result], [("good.md", "")])

    def test_keyword_query_removes_stopwords_duplicates_and_bounds_conjunction(self):
        self.assertEqual(qmd_search.keyword_query("How should we organise our vault note types and graph view?"),
                         "organise vault note types")
        self.assertEqual(qmd_search.keyword_query("retry retry timeout"), "retry timeout")
        self.assertEqual(qmd_search.keyword_query("mule-review case triage"), "mule review case triage")
        with mock.patch.object(qmd_search, "_qmd_bin", return_value=""):
            self.assertFalse(qmd_search.is_qmd_available())

    def test_path_resolution_rejects_wrong_scope_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "vault"
            root.mkdir()
            (root / "linked").symlink_to(Path(tmp), target_is_directory=True)
            self.assertEqual(qmd_search.resolve_result_path("qmd://notes/topics/retry.md", root, "notes"),
                             root.resolve() / "topics/retry.md")
            for bad in ("qmd://other/retry.md", "qmd://notes/../outside.md",
                        "qmd://notes/%2e%2e/outside.md", "../outside.md", "linked/outside.md",
                        "qmd://notes/a.md?scope=other", "https://example.com/a.md", "data.json"):
                self.assertIsNone(qmd_search.resolve_result_path(bad, root, "notes"), bad)
            self.assertIsNone(qmd_search.resolve_result_path("qmd://notes/retry.md", root))


class SynthesisRetrievalTests(unittest.TestCase):
    def test_prompt_retains_conclusions_and_actual_tool_outcomes(self):
        transcript = transcript_reader.TranscriptContent(
            session_id="s1", user_prompts=["Investigate retry behavior"],
            assistant_texts=["Proposed increasing retries.", "Verified the current failure is authentication."],
            tool_uses=[transcript_reader.ToolUse("exec_command", {"cmd": "check"}, "401 unauthorized", False)],
        )
        with mock.patch.object(config, "QMD_SYNTH_ENABLED", False):
            prompt = synthesizer.build_synthesis_prompt(transcript, vault_indexer.VaultIndex())
        self.assertIn("Verified the current failure is authentication.", prompt)
        self.assertIn("Outcome (failed): 401 unauthorized", prompt)
        self.assertIn("An assistant proposal", prompt)
        self.assertLessEqual(len(synthesizer._format_assistant_texts(["x" * 30000] * 100)), 12100)
        prompts = ["Original goal"] + ["Earlier investigation " * 25] * 100 + ["Correction: retain the actual result"]
        formatted = synthesizer._format_user_prompts(prompts)
        self.assertIn("Original goal", formatted)
        self.assertIn("Correction: retain the actual result", formatted)

    def test_requires_explicit_collection_and_uses_full_local_note(self):
        transcript = transcript_reader.TranscriptContent(session_id="s1", user_prompts=["retry timeout"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "retry.md"
            note.write_text("# Retry\n\nVerified durable evidence beyond a search snippet.")
            index = vault_indexer.VaultIndex(notes={"retry.md": vault_indexer.NoteIndex(path="retry.md", title="Retry")})
            result = qmd_search.SearchResult("qmd://notes/retry.md", "Retry", 0.05, "unverified snippet")
            with mock.patch.multiple(config, VAULT_ROOT=root, QMD_SYNTH_ENABLED=True, QMD_COLLECTION="", QMD_SEARCH_MODE="keyword"), \
                    mock.patch.object(qmd_search, "search_keyword", return_value=[result]) as search, \
                    mock.patch.object(qmd_search, "is_qmd_available", return_value=True):
                self.assertIn("collection not configured", synthesizer._get_related_note_snippets(transcript, index))
                search.assert_not_called()
                with mock.patch.object(config, "QMD_COLLECTION", "notes"):
                    context = synthesizer._get_related_note_snippets(transcript, index)
                self.assertIn("Verified durable evidence", context)
                self.assertNotIn("unverified snippet", context)
                self.assertNotIn("score:", context)
                self.assertEqual(search.call_args.args[0], "retry timeout")

    def test_wrong_collection_and_missing_local_source_are_not_injected(self):
        transcript = transcript_reader.TranscriptContent(session_id="s1", user_prompts=["retry"])
        results = [qmd_search.SearchResult("qmd://other/retry.md", "Private", 1, "other tenant"),
                   qmd_search.SearchResult("qmd://notes/missing.md", "Gone", 1, "stale source")]
        with mock.patch.multiple(config, QMD_SYNTH_ENABLED=True, QMD_COLLECTION="notes", QMD_SEARCH_MODE="keyword"), \
                mock.patch.object(qmd_search, "search_keyword", return_value=results), \
                mock.patch.object(qmd_search, "is_qmd_available", return_value=True):
            context = synthesizer._get_related_note_snippets(transcript, vault_indexer.VaultIndex())
        self.assertNotIn("other tenant", context)
        self.assertNotIn("stale source", context)

    def test_synthesis_honors_configured_timeout(self):
        transcript = transcript_reader.TranscriptContent(session_id="s1")
        pack = mock.Mock(time="", assistant="")
        with mock.patch.object(config, "SYNTH_TIMEOUT", 271), \
                mock.patch.object(synthesizer, "build_synthesis_prompt", return_value="prompt"), \
                mock.patch.object(synthesizer, "_claude_bin", return_value="claude"), \
                mock.patch.object(synthesizer, "parse_knowledge_pack", return_value=pack), \
                mock.patch.object(synthesizer.knowledge_pack, "validate_knowledge_pack", return_value=[]), \
                mock.patch.object(synthesizer.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="{}")) as run:
            synthesizer.synthesize_session(transcript, vault_indexer.VaultIndex())
        self.assertEqual(run.call_args.kwargs["timeout"], 271)


class ConfigAliasTests(unittest.TestCase):
    def test_documented_vault_env_overrides_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            xdg = Path(tmp) / "config"
            (xdg / "claude-note").mkdir(parents=True)
            (xdg / "claude-note/config.toml").write_text('vault_root = "/configured-vault"\n')
            env = dict(os.environ, XDG_CONFIG_HOME=str(xdg), CLAUDE_NOTE_VAULT=str(Path(tmp) / "override"))
            result = subprocess.run(["python3", "-c", "from claude_note.config import VAULT_ROOT; print(VAULT_ROOT)"],
                                    env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), str(Path(tmp) / "override"))


if __name__ == "__main__":
    unittest.main()
