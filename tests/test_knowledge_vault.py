"""Knowledge quality and safe-write regressions, using disposable vaults only."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import cleaner, config, ingest, knowledge_pack, managed_blocks, models, note_router, note_writer, provenance, push, qmd_search


class VaultTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = (Path(self.temp.name) / "vault").resolve()
        self.vault.mkdir()
        for name, value in {
            "VAULT_ROOT": self.vault,
            "INBOX_PATH": self.vault / "claude-note-inbox.md",
            "STATE_DIR": Path(self.temp.name) / "state",
            "INTERNAL_DIR": self.vault / "internal",
            "LITERATURE_DIR": self.vault / "literature",
            "QMD_COLLECTION": "test-vault",
            "QMD_SYNTH_ENABLED": True,
            "QMD_SEARCH_MODE": "keyword",
            "QMD_INGEST_DEDUP_ENABLED": True,
            "INBOX_DEDUP_ENABLED": True,
        }.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(provenance, "author_email", return_value="author@example.com")
        patcher.start()
        self.addCleanup(patcher.stop)

    def pack(self, **kwargs):
        return knowledge_pack.KnowledgePack(session_id="session-1", date="2026-10-10", title="Retry behavior", **kwargs)


class KnowledgeTypeTests(VaultTestCase):
    def test_all_semantic_roles_are_preserved_without_widening_push(self):
        for note_type in knowledge_pack.NOTE_TYPES:
            with self.subTest(note_type=note_type):
                result = note_router.with_type({"tags": ["reference"], "type": note_type})
                self.assertEqual(result["type"], note_type)
                self.assertEqual(next(iter(result)), "type")
        for private_type in ("source", "meta", "session", "feedback", "log"):
            self.assertNotIn(private_type, push.PUSH_TYPES)

    def test_missing_type_warns_and_legacy_exact_tag_is_preserved(self):
        with self.assertLogs(note_router.logger, level="WARNING") as logs:
            self.assertEqual(note_router.with_type({"tags": ["gotcha"]})["type"], "gotcha")
            self.assertEqual(note_router.with_type({"type": ["gotcha"], "tags": []})["type"], "reference")
        self.assertEqual(len(logs.output), 2)
        warnings = knowledge_pack.validate_knowledge_pack(self.pack(note_ops=[
            knowledge_pack.NoteOp("create", "failure", "body", {"tags": ["gotcha"]})]))
        self.assertTrue(any("explicit note type" in warning for warning in warnings))

    def test_note_ops_alone_are_not_empty_and_roundtrip_keeps_provenance(self):
        pack = self.pack(note_ops=[knowledge_pack.NoteOp("create", "failure", "body", {"type": "gotcha"})], assistant="codex")
        self.assertFalse(pack.is_empty())
        self.assertEqual(knowledge_pack.KnowledgePack.from_json(pack.to_json()).assistant, "codex")

    def test_frontmatter_escapes_strings_without_creating_extra_fields(self):
        value = 'A: "quoted" name\nsecret_field: true'
        text = note_router._format_frontmatter({"type": "reference", "title": value, "tags": [value]})
        title_line = next(line for line in text.splitlines() if line.startswith("title:"))
        self.assertEqual(json.loads(title_line.split(":", 1)[1]), value)
        self.assertNotIn("\nsecret_field:", text)
        with self.assertRaises(ValueError):
            note_router._format_frontmatter({"title\nsecret_field": "value"})


class SafeRouteTests(VaultTestCase):
    def test_legacy_session_evidence_is_reserved_but_curated_session_topics_are_usable(self):
        state = models.SessionState(session_id="legacy-session", first_event_ts="2026-10-10T10:00:00Z",
                                    last_event_ts="2026-10-10T10:01:00Z")
        legacy = self.vault / note_writer.get_note_filename(state)
        original = "---\ntype: session\n---\n\nOriginal session evidence.\n"
        legacy.write_text(original)
        for path in (legacy.name, legacy.stem):
            for operation in ("create", "append", "upsert_block"):
                with self.subTest(path=path, operation=operation):
                    op = knowledge_pack.NoteOp(operation, path, "Model replacement", {"type": "reference"},
                                               managed_block_id="model-update")
                    self.assertFalse(note_router.apply_note_op(op, self.vault, session_id="s1"))
                    self.assertEqual(legacy.read_text(), original)
        curated = knowledge_pack.NoteOp("create", "claude-session-workflow", "Reusable capture workflow",
                                       {"type": "pattern"})
        self.assertTrue(note_router.apply_note_op(curated, self.vault, session_id="s1"))
        self.assertTrue((self.vault / "claude-session-workflow.md").exists())

    def test_rejects_escape_settings_instructions_and_nonmarkdown_for_every_op(self):
        paths = ("../outside.md", "/tmp/outside.md", "a/../../outside.md", "a\\outside.md", ".obsidian/graph.md",
                 ".claude-note/state.md", "templates/topic.md", "sessions/raw.md", "CLAUDE.md", "a/AGENTS.md", "CLAUDE", "a/AGENTS",
                 "runner.py", "a\nfield.md", "", "notes/./idea.md")
        for path in paths:
            for op in ("create", "append", "upsert_block"):
                with self.subTest(path=path, op=op):
                    self.assertFalse(note_router.apply_note_op(knowledge_pack.NoteOp(op, path, "body", {"type": "reference"}), self.vault))
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_symlink_routes_cannot_touch_outside_files(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        file = outside / "existing.md"
        file.write_text("human content")
        (self.vault / "linked").symlink_to(outside, target_is_directory=True)
        (self.vault / "alias.md").symlink_to(file)
        for path in ("linked/new", "linked/existing", "alias"):
            self.assertFalse(note_router.apply_note_op(knowledge_pack.NoteOp("create", path, "replacement", {"type": "reference"}), self.vault))
        self.assertEqual(file.read_text(), "human content")
        self.assertFalse((outside / "new.md").exists())

    def test_nested_create_is_exclusive_and_does_not_follow_predictable_temp(self):
        path = note_router.create_note("concepts/retry", {"type": "gotcha"}, "body", self.vault)
        self.assertEqual(path.read_text().splitlines()[1], 'type: "gotcha"')
        with self.assertRaises(FileExistsError):
            note_router.create_note("concepts/retry", {"type": "reference"}, "replace", self.vault)
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("keep")
        (self.vault / "new.tmp").symlink_to(outside)
        note_router.create_note("new", {"type": "reference"}, "new knowledge", self.vault)
        self.assertEqual(outside.read_text(), "keep")

    def test_create_on_existing_preserves_human_type_and_uses_full_session_identity(self):
        note = self.vault / "topic.md"
        note.write_text("---\ntype: decision\nauthor: human@example.com\n---\n\nHuman rationale.\n")
        op = knowledge_pack.NoteOp("create", "topic", "First observation", {"type": "gotcha"})
        note_router.apply_note_op(op, self.vault, session_id="sameprefix-session-a", assistant="codex")
        op.body_markdown = "Second observation"
        note_router.apply_note_op(op, self.vault, session_id="sameprefix-session-b", assistant="codex")
        text = note.read_text()
        self.assertIn("Human rationale.", text)
        self.assertIn("First observation", text)
        self.assertIn("Second observation", text)
        self.assertEqual(push.frontmatter(text)["type"], "decision")
        self.assertEqual(push.frontmatter(text)["author"], "human@example.com")

    def test_append_retries_are_idempotent_and_corrections_are_distinct(self):
        note = note_router.create_note("retry", {"type": "gotcha"}, "Human notes.\n\n## Evidence\n\nOriginal.\n\n## Next\n\nKeep.\n", self.vault)
        op = knowledge_pack.NoteOp("append", "retry", "New evidence", section="## Evidence")
        for _ in range(2):
            self.assertTrue(note_router.apply_note_op(op, self.vault, session_id="s1"))
        op.body_markdown = "Correction: response can arrive late"
        self.assertTrue(note_router.apply_note_op(op, self.vault, session_id="s1"))
        text = note.read_text()
        self.assertEqual(text.count("New evidence"), 1)
        self.assertEqual(text.count("Correction:"), 1)
        self.assertLess(text.index("Correction:"), text.index("## Next"))
        self.assertIn("Human notes.", text)

    def test_create_retry_does_not_duplicate_the_initial_body(self):
        op = knowledge_pack.NoteOp("create", "new-topic", "Generated knowledge", {"type": "pattern"})
        for _ in range(2):
            self.assertTrue(note_router.apply_note_op(op, self.vault, session_id="s1"))
        self.assertEqual((self.vault / "new-topic.md").read_text().count("Generated knowledge"), 1)

    def test_invalid_block_id_is_rejected(self):
        op = knowledge_pack.NoteOp("create", "topic", "body", {"type": "reference"}, "bad:start -->")
        self.assertFalse(note_router.apply_note_op(op, self.vault))
        self.assertFalse((self.vault / "topic.md").exists())

    def test_route_to_existing_reports_update_and_respects_override_vault(self):
        other_vault = Path(self.temp.name) / "override"
        other_vault.mkdir()
        (other_vault / "topic.md").write_text("human")
        pack = self.pack(note_ops=[knowledge_pack.NoteOp("create", "topic", "new", {"type": "reference"})])
        result = note_router.apply_note_ops(pack, "route", other_vault)
        self.assertEqual(result["notes_updated"], ["topic"])
        self.assertEqual(result["notes_created"], [])
        self.assertTrue((other_vault / "claude-note-inbox.md").exists())
        self.assertFalse(config.INBOX_PATH.exists())


class InboxAndSessionTests(VaultTestCase):
    def test_exact_duplicate_skipped_but_same_subject_correction_survives(self):
        pack = self.pack(highlights=["Retries are safe after a timeout"])
        with mock.patch.object(qmd_search, "search_vector") as search:
            self.assertIsNotNone(note_router.append_to_inbox(pack))
            self.assertIsNone(note_router.append_to_inbox(pack))
            pack.highlights = ["Correction: retry requires an idempotency key"]
            self.assertIsNotNone(note_router.append_to_inbox(pack))
            search.assert_not_called()
        text = config.INBOX_PATH.read_text()
        self.assertEqual(text.count("## 2026-10-10"), 2)
        self.assertEqual(push.frontmatter(text)["type"], "meta")

    def test_new_sessions_are_typed_and_legacy_files_keep_manual_edits(self):
        state = models.SessionState(session_id="session-a", first_event_ts="2026-10-10T10:00:00Z", last_event_ts="2026-10-10T10:01:00Z")
        path = note_writer.write_session_note(state)
        self.assertEqual(path.parent, self.vault / "sessions")
        self.assertEqual(push.frontmatter(path.read_text())["type"], "session")
        self.assertNotIn("[[obsidian-workflow]]", path.read_text())
        state.session_id = "legacy-b"
        legacy = self.vault / note_writer.get_note_filename(state)
        legacy.write_text("# Legacy\n\n## Summary\n\nManual summary.\n\n## Timeline\n\nold\n\n## Decisions\n\nManual decision.\n")
        self.assertEqual(note_writer.update_session_note(state), legacy)
        self.assertIn("Manual summary.", legacy.read_text())
        self.assertIn("Manual decision.", legacy.read_text())
        self.assertFalse((self.vault / "sessions" / legacy.name).exists())

    def test_session_ids_are_safe_filename_components(self):
        state = models.SessionState(session_id="../../escape", first_event_ts="2026-10-10T10:00:00Z", last_event_ts="2026-10-10T10:01:00Z")
        path = note_writer.write_session_note(state)
        self.assertEqual(path.parent, self.vault / "sessions")
        self.assertNotIn("..", path.name)

    def test_keyword_link_enhancement_respects_disabled_qmd(self):
        target = self.vault / "retry-reference.md"
        target.write_text("Verified source")
        pack = self.pack(concepts=[knowledge_pack.Concept("Retries", "Retry evidence")])
        result = qmd_search.SearchResult("qmd://test-vault/retry-reference.md", "Retries", .8)
        with mock.patch.object(qmd_search, "is_qmd_available", return_value=True), \
                mock.patch.object(qmd_search, "search_keyword", return_value=[result]) as keyword, \
                mock.patch.object(qmd_search, "search_vector") as vector:
            note_router._enhance_concept_links(pack)
            self.assertEqual(pack.concepts[0].links_suggested, ["retry-reference"])
            keyword.assert_called_once()
            vector.assert_not_called()
            with mock.patch.object(config, "QMD_SYNTH_ENABLED", False):
                note_router._enhance_concept_links(pack)
            self.assertEqual(keyword.call_count, 1)
            vector.assert_not_called()


class IngestQualityTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.literature = config.LITERATURE_DIR
        self.literature.mkdir()
        self.source = Path(self.temp.name) / 'study "quoted".txt'
        self.source.write_text("Verified source")
        self.extraction = {"key_citation": "Study 2026", "source_summary": "Evidence.", "source_type": "paper", "notes": []}

    def test_ingestion_has_no_invented_project_context(self):
        self.assertNotIn('"Fi"', ingest.LITERATURE_EXTRACTION_PROMPT)
        self.assertNotIn('"Fi"', ingest.INTERNAL_EXTRACTION_PROMPT)
        concept = {"slug": "retry-evidence", "summary": "Observed result", "tags": ["retries"]}
        with mock.patch.object(ingest, "_find_similar_existing_concept", return_value=None):
            path = ingest.create_concept_note(concept, "Study 2026", self.literature, "2026-10-10")
        text = path.read_text()
        self.assertEqual(push.frontmatter(text)["type"], "literature")
        self.assertEqual(push.frontmatter(text)["author"], "author@example.com")
        self.assertNotIn("project/fi", text)
        self.assertNotIn("fi-moc", text)
        self.assertIn("## Source", text)

    def test_source_index_links_only_real_files_and_preserves_manual_text(self):
        actual = self.literature / "lit-existing-concept.md"
        actual.write_text("existing")
        missing = self.literature / "lit-missing.md"
        path = ingest.create_source_note(self.extraction, self.source, self.literature, "2026-10-10", concept_paths=[actual, missing])
        path.write_text(path.read_text() + "\nHuman annotation.\n")
        self.extraction["source_summary"] = "Updated evidence."
        ingest.create_source_note(self.extraction, self.source, self.literature, "2026-10-11", concept_paths=[actual])
        text = path.read_text()
        self.assertIn("[[literature/lit-existing-concept]]", text)
        self.assertNotIn("lit-missing", text)
        self.assertIn("Human annotation.", text)
        self.assertEqual(text.count("Updated evidence."), 1)
        self.assertNotIn("\nEvidence.\n", text)

    def test_source_file_collision_never_replaces_an_existing_note(self):
        path = self.literature / "lit-study-2026.md"
        path.write_text("Human concept with same name.")
        with self.assertRaises(FileExistsError):
            ingest.create_source_note(self.extraction, self.source, self.literature, "2026-10-10")
        self.assertEqual(path.read_text(), "Human concept with same name.")

    def test_internal_ingest_uses_source_type_and_model_slugs_cannot_escape(self):
        config.INTERNAL_DIR.mkdir()
        with mock.patch.object(ingest, "_find_similar_existing_concept", return_value=None):
            path = ingest.create_concept_note({"slug": "runbook", "summary": "Procedure"}, "Runbook", config.INTERNAL_DIR, "2026-10-10", mode="internal")
        self.assertEqual(push.frontmatter(path.read_text())["type"], "source")
        for slug in ("../outside", "/absolute", "a/b", "name\nfield", "a--b"):
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                ingest.create_concept_note({"slug": slug}, "Study", self.literature, "2026-10-10")

    def test_ingest_source_and_same_named_concept_are_distinct(self):
        self.extraction["notes"] = [{"slug": "study-2026", "summary": "A concept"}]
        with mock.patch.object(ingest, "extract_knowledge", return_value=self.extraction), mock.patch.object(ingest, "_find_similar_existing_concept", return_value=None):
            result = ingest.ingest_document(self.source)
        self.assertEqual(result["source_note"].name, "lit-study-2026.md")
        self.assertEqual(result["concept_notes"][0].name, "lit-study-2026-concept.md")
        self.assertIn("[[literature/lit-study-2026-concept]]", result["source_note"].read_text())
        self.assertIn("[[literature/lit-study-2026]]", result["concept_notes"][0].read_text())

    def test_qmd_merge_requires_collection_and_real_destination(self):
        note = self.literature / "lit-existing.md"
        note.write_text("---\ntype: literature\n---\nconcept")
        results = [qmd_search.SearchResult("qmd://other/literature/lit-existing.md", "foreign", .99),
                   qmd_search.SearchResult("qmd://test-vault/literature/lit-existing.md", "same", .8)]
        with mock.patch.object(qmd_search, "is_qmd_available", return_value=True), mock.patch.object(qmd_search, "search_vector", return_value=results) as search:
            self.assertEqual(ingest._find_similar_existing_concept({"title": "concept"}, self.literature), note)
            self.assertEqual(search.call_args.kwargs["collection"], "test-vault")
            with mock.patch.object(config, "QMD_COLLECTION", ""):
                self.assertIsNone(ingest._find_similar_existing_concept({"title": "concept"}, self.literature))
            with mock.patch.object(config, "QMD_SYNTH_ENABLED", False):
                self.assertIsNone(ingest._find_similar_existing_concept({"title": "concept"}, self.literature))
            self.assertEqual(search.call_count, 1)

    def test_dry_run_does_not_create_output_directory(self):
        with mock.patch.object(config, "INTERNAL_DIR", self.vault / "new-internal"), mock.patch.object(ingest, "extract_knowledge", return_value=self.extraction):
            ingest.ingest_document(self.source, dry_run=True, mode="internal")
            self.assertFalse(config.INTERNAL_DIR.exists())


class CleanerPreservationTests(VaultTestCase):
    def test_matching_titles_never_remove_a_correction(self):
        first = self.pack(highlights=["Retries are safe after a timeout"])
        correction = self.pack(highlights=["Correction: use an idempotency key before retrying"])
        note_router.append_to_inbox(first, skip_dedup=True)
        note_router.append_to_inbox(correction, skip_dedup=True)
        result = cleaner.dedupe_inbox(similarity_threshold=0, dry_run=False)
        self.assertEqual(result["entries_removed"], 0)
        self.assertIn("Correction:", config.INBOX_PATH.read_text())

    def test_fingerprint_duplicates_remove_only_identical_unannotated_entries(self):
        pack = self.pack(highlights=["Verified finding"])
        entry = note_router.format_inbox_entry(pack)
        config.INBOX_PATH.write_text("# Inbox\n\n" + entry + entry + entry + "Human annotation.\n")
        result = cleaner.dedupe_inbox(dry_run=False)
        self.assertEqual(result["entries_removed"], 1)
        self.assertEqual(config.INBOX_PATH.read_text().count("Verified finding"), 2)
        self.assertIn("Human annotation.", config.INBOX_PATH.read_text())

    def test_legacy_exact_body_duplicates_keep_latest_entry(self):
        config.INBOX_PATH.write_text("# Inbox\n\n## 2026-10-09 - Old title\n\nSame evidence.\n\n## 2026-10-10 - New title\n\nSame evidence.\n")
        result = cleaner.dedupe_inbox(dry_run=False)
        self.assertEqual(result["entries_removed"], 1)
        self.assertIn("New title", config.INBOX_PATH.read_text())
        self.assertNotIn("Old title", config.INBOX_PATH.read_text())

    def test_pending_retry_and_unreadable_states_survive_retention(self):
        config.STATE_DIR.mkdir()
        pending = config.STATE_DIR / "pending.json"
        pending.write_text('{"synthesis_pending": true}')
        completed = config.STATE_DIR / "completed.json"
        completed.write_text('{"synthesis_pending": false}')
        malformed = config.STATE_DIR / "malformed.json"
        malformed.write_text("unreadable")
        old = time.time() - 10 * 86400
        for path in (pending, completed, malformed):
            os.utime(path, (old, old))
        result = cleaner.clean_state_dir(dry_run=False)
        self.assertEqual(result["states_removed"], 1)
        self.assertTrue(pending.exists())
        self.assertTrue(malformed.exists())
        self.assertFalse(completed.exists())

    def test_session_discovery_covers_both_layouts(self):
        root_note = self.vault / "claude-session-2026-10-10-legacy.md"
        root_note.write_text("old layout")
        (self.vault / "sessions").mkdir()
        nested_note = self.vault / "sessions/claude-session-2026-10-10-current.md"
        nested_note.write_text("new layout")
        self.assertEqual(set(cleaner.find_session_notes("2026-10-10")), {root_note, nested_note})

    def test_block_cleanup_preserves_similar_but_contradictory_evidence(self):
        note = self.vault / "retries.md"
        note.write_text("Human notes.\n")
        old = "Retry requires the same operation id and the server guarantees exactly one committed operation after a timeout."
        correction = "Retry requires the same operation id and the server does not guarantee exactly one committed operation after a timeout."
        for block_id, text in (("first", old), ("correction", correction), ("duplicate", old)):
            managed_blocks.write_managed_block(note, block_id, text, create_if_missing=True)
        result = cleaner.consolidate_managed_blocks(note, dry_run=False)
        self.assertEqual(result["blocks_removed"], 1)
        self.assertIn(correction, note.read_text())
        self.assertEqual(note.read_text().count(old), 1)


if __name__ == "__main__":
    unittest.main()
