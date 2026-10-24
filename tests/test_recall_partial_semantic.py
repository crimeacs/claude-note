"""Incomplete public server coverage must not become a complete search miss."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_note import recall


class PartialSemanticCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "oauth.md"
        self.source.write_text(
            "---\ntype: gotcha\n---\nOAuth retry requires reusing the original token "
            "identity and verifying permissions before another request.\n"
        )
        self.settings = recall.Settings(
            engine="server", collections=["public-fixture"], memory_collections=[],
            vector=True, rerank=False, log=False,
        )

    def response(self, *, partial=True, rows=None, coverage_partial=False):
        value = {
            "lex": [rows or []], "vec": [], "partial": partial,
            "coverage": {"lexical": "complete_scoped_before_limit",
                         "semantic": "partial_deadline" if partial else "complete_scoped_exact_vectors"},
            "manifest": {"index": {"id": "default"}},
        }
        if coverage_partial:
            value["coverage"]["partial"] = True
        return value

    def run_response(self, response):
        with mock.patch.object(recall, "server_post", return_value=response), \
                mock.patch.object(recall, "local_path", return_value=self.source), \
                mock.patch.object(recall, "_run_qmd", side_effect=AssertionError("No CLI fallback")):
            return recall.recall("OAuth retry token identity", self.settings)

    def row(self):
        return {"file": "qmd://public-fixture/oauth.md", "title": "OAuth retry token identity",
                "snippet": self.source.read_text(), "score": .8}

    def test_partial_empty_success_response_is_unavailable(self):
        result = self.run_response(self.response())
        self.assertEqual(result.status, "unavailable")
        self.assertIs(result.coverage.get("partial"), True)
        self.assertIn("coverage incomplete", result.reason)
        self.assertEqual(result.attempted_engines, ["server"])

    def test_partial_candidates_without_eligible_passages_are_unavailable(self):
        response = self.response(rows=[self.row()])
        self.source.write_text("---\ntype: gotcha\n---\n")
        result = self.run_response(response)
        self.assertEqual(result.status, "unavailable")
        self.assertIs(result.coverage.get("partial"), True)
        self.assertIn("no eligible source", result.reason)

    def test_valid_partial_evidence_remains_a_hit_with_incomplete_coverage(self):
        result = self.run_response(self.response(rows=[self.row()]))
        self.assertEqual(result.status, "hit")
        self.assertIs(result.coverage.get("partial"), True)
        self.assertEqual([hit.uri for hit in result.hits], ["qmd://public-fixture/oauth.md"])

    def test_existing_coverage_partial_flag_is_not_lost(self):
        result = self.run_response(self.response(partial=False, coverage_partial=True))
        self.assertEqual(result.status, "unavailable")
        self.assertIs(result.coverage.get("partial"), True)

    def test_complete_empty_server_response_remains_a_miss(self):
        result = self.run_response(self.response(partial=False))
        self.assertEqual(result.status, "miss")
        self.assertIsNot(result.coverage.get("partial"), True)


if __name__ == "__main__":
    unittest.main()
