"""Growing public transcripts exercise publication, not only extraction deltas."""

import hashlib
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_note import (config, egress, import_publication as publication,
                         importers, models, queue_manager, session_tracker,
                         source_store, transcript_reader, worker)


def row(number, *, role="user", text=None):
    text = text if text is not None else "Public synthetic evidence " + str(number) + " x" * 2048
    return (json.dumps({"type": role, "uuid": "public-" + str(number),
        "sessionId": "publicsession", "cwd": "/public-synthetic-project",
        "entrypoint": "cli", "message": {"role": role,
        "content": [{"type": "text", "text": text}] if role == "assistant" else text}})
        + "\n").encode()


def transcript():
    return b"".join(row(i, role="assistant" if i % 2 else "user") for i in range(160))


class SourceStorageIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name, value in (("STATE_DIR", self.root / "state"),
                            ("QUEUE_DIR", self.root / "queue")):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(Path, "home", return_value=self.root / "home")
        patch.start()
        self.addCleanup(patch.stop)
        self.path = self.root / "projects" / "project" / "publicsession.jsonl"
        self.path.parent.mkdir(parents=True)
        self.raw = transcript()
        self.path.write_bytes(self.raw)

    def publish(self, raw=None, **kwargs):
        raw = self.raw if raw is None else raw
        return publication.publish("claude-code", "publicsession", self.path.name,
            self.path, raw, "", "Public synthetic question", **kwargs)

    def record(self, result):
        return publication.load(result.publication_id)

    def parts(self, result):
        return source_store.segments(result.snapshot_path, result.captured_revision,
                                      scope=publication.source_scope(self.path))

    def events(self, result):
        return list(queue_manager.read_events(result.queue_path))

    def test_repeated_native_sweep_growth_shares_prefix_before_model_success(self):
        seen, expected, records = {}, [], []
        raw = self.raw
        for number in range(12):
            if number:
                raw += row(200 + number, text="Public appended turn " + str(number))
            self.path.write_bytes(raw)
            os.utime(self.path, (1000 + number, 1000 + number))
            with mock.patch.object(importers.time, "time", return_value=2000):
                self.assertEqual(importers.sweep_claude_code(seen, 0,
                    projects_dir=self.path.parent.parent), 1)
            expected.append(raw)
        for path in (config.STATE_DIR / "import-publications").glob("*.json"):
            if len(path.stem) == 64:
                records.append(publication.load(path.stem))
        self.assertEqual(len(records), 12)
        self.assertTrue(all(record["version"] == 2 for record in records))
        by_hash = {hashlib.sha256(raw).hexdigest(): raw for raw in expected}
        for record in records:
            self.assertEqual(publication._read_source(record), by_hash[record["captured_revision"]])
        stored = sum(path.stat().st_size for path in
            (config.STATE_DIR / "sources" / "objects").rglob("*") if path.is_file())
        naive = sum(map(len, expected))
        self.assertLess(stored, naive // 2, "Publication must share old prefixes before extraction succeeds")
        self.assertEqual(len(list(queue_manager.read_pending_events())), 24)
        self.assertFalse(session_tracker.get_state_file("publicsession").exists())

    def test_reader_keeps_exact_real_chunk_anchors_after_original_is_deleted(self):
        result = self.publish()
        self.assertEqual(self.record(result)["version"], 2)
        state = session_tracker.update_session_from_events("publicsession", self.events(result))
        session_tracker.save_session_state(state)
        self.path.unlink()
        content = transcript_reader.read_transcript_from_state(state)
        self.assertEqual(content.source_revision, result.captured_revision)
        self.assertEqual(len(content.source_events), 160)
        for original_line, event in enumerate(content.source_events, 1):
            evidence = Path(event.source).read_bytes()
            self.assertEqual(hashlib.sha256(evidence).hexdigest(), event.revision)
            self.assertEqual(hashlib.sha256(evidence.splitlines()[event.line - 1]).hexdigest(), event.payload_digest)
            self.assertEqual((event.origin_source, event.origin_revision, event.origin_line),
                             (str(self.path), result.captured_revision, original_line))
            self.assertEqual(Path(event.source).suffix, ".jsonl")
        self.assertFalse((config.STATE_DIR / "sources" / (result.captured_revision + ".jsonl")).exists())

    def test_codex_rollout_sweep_uses_chunked_publication_for_growing_revision(self):
        sessions = self.root / "codex-sessions"
        path = sessions / "2026" / "01" / "01" / "rollout-public.jsonl"
        path.parent.mkdir(parents=True)
        raw = (json.dumps({"type": "session_meta", "payload": {"id": "publiccodex",
            "cwd": "/public-synthetic-project", "source": "cli"}}) + "\n").encode()
        raw += b"".join((json.dumps({"type": "response_item", "payload": {"type": "message",
            "role": "user" if i % 2 else "assistant", "content": [{"type": "input_text" if i % 2 else "output_text",
            "text": "Public Codex evidence " + str(i) + " x" * 2048}]}}) + "\n").encode()
            for i in range(100))
        seen = {}
        for number in range(2):
            path.write_bytes(raw)
            os.utime(path, (1000 + number, 1000 + number))
            with mock.patch.object(importers.time, "time", return_value=2000):
                self.assertEqual(importers.sweep_codex(seen, 0, sessions_dir=sessions), 1)
            raw += (json.dumps({"type": "response_item", "payload": {"type": "message",
                "role": "user", "content": [{"type": "input_text", "text": "Appended public turn"}]}}) + "\n").encode()
        records = [publication.load(p.stem) for p in (config.STATE_DIR / "import-publications").glob("*.json") if len(p.stem) == 64]
        self.assertEqual(len(records), 2)
        self.assertEqual({record["version"] for record in records}, {2})
        self.assertEqual(len(list(queue_manager.read_pending_events())), 4)

    def test_changed_old_chunk_with_same_size_and_mtime_blocks_newer_delivery(self):
        first = self.publish()
        newer = self.raw + row(200, text="Newer public evidence")
        self.path.write_bytes(newer)
        self.publish(newer)
        part = Path(self.parts(first)[0]["path"])
        before = part.stat()
        old = part.read_bytes()
        part.write_bytes(old.replace(b"Public", b"Broken", 1))
        os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(list(queue_manager.read_pending_events()), [])
        with mock.patch.object(worker.note_writer, "update_session_note") as writer, \
                mock.patch.object(worker, "run_synthesis") as model:
            events = [models.QueuedEvent(**event) for event in self.record_without_source(first)["events"]]
            self.assertFalse(worker.process_session("publicsession", events, logging.getLogger("chunk-test")))
        writer.assert_not_called()
        model.assert_not_called()
        self.assertFalse(session_tracker.get_state_file("publicsession").exists())

    def record_without_source(self, result):
        return publication.load(result.publication_id, verify_source=False)

    def test_every_tail_chunk_keeps_private_origin_after_origin_looks_public(self):
        private = self.root / "origin.md"
        private.write_text("---\nprivate: true\n---\nPrivate synthetic source control.")
        result = self.publish(origin_sources=[private])
        private.write_text("Now appears public")
        for part in self.parts(result):
            with self.assertRaises(egress.EgressDenied):
                egress.prepare("Public synthetic derived payload", audience="model",
                    sources=[str(part["path"])], rules={"default": ["model"]})

    def test_privacy_failure_never_exposes_queue_and_replay_reuses_first_intent(self):
        with mock.patch.object(egress, "inherit_capture_restrictions", side_effect=ValueError("public test failure")):
            with self.assertRaises(ValueError):
                self.publish()
        self.assertEqual(list(queue_manager.read_queue_files()), [])
        intent = next(p for p in (config.STATE_DIR / "import-intents").glob("*.json") if len(p.stem) == 64)
        first = json.loads(intent.read_text())
        result = self.publish()
        self.assertEqual(self.events(result)[0].ts, first["events"][0]["ts"])
        self.assertEqual(self.record(result)["version"], 2)

    def test_failed_storage_admission_does_not_advance_seen_or_queue(self):
        os.utime(self.path, (1000, 1000))
        seen = {}
        with mock.patch.object(importers.time, "time", return_value=2000), \
                mock.patch.object(source_store, "publish", side_effect=ValueError("Evidence storage headroom unavailable")):
            problems = []
            self.assertEqual(importers.sweep_claude_code(seen, 0,
                projects_dir=self.path.parent.parent, problems=problems), 0)
            self.assertEqual(len(problems), 1)
        self.assertEqual(seen, {})
        self.assertEqual(list(queue_manager.read_queue_files()), [])

    def test_source_lineage_budget_denial_retains_evidence_without_queue_visibility(self):
        capture = egress.capture_restrictions
        def deny_lineage(*args, **kwargs):
            with mock.patch.object(source_store, "auxiliary_admission", side_effect=ValueError("Synthetic lineage quota")):
                return capture(*args, **kwargs)
        with mock.patch.object(egress, "capture_restrictions", side_effect=deny_lineage):
            with self.assertRaises(ValueError):
                self.publish()
        self.assertEqual(list(queue_manager.read_queue_files()), [])
        self.assertTrue(list((config.STATE_DIR / "sources" / "objects").rglob("*.source.json")))
        self.assertTrue(list((config.STATE_DIR / "import-intents").glob("*.json")))

    def test_new_identity_cannot_bypass_headroom_with_already_shared_evidence(self):
        first = self.publish()
        counter = next((config.STATE_DIR / "import-streams").glob("*.json"))
        before = counter.read_bytes()
        with mock.patch.object(source_store, "_free_bytes", return_value=0):
            with self.assertRaises(ValueError):
                publication.publish("claude-code", "publicsession", "another-logical-id",
                    self.path, self.raw, "", "Public synthetic question")
        self.assertEqual(counter.read_bytes(), before)
        self.assertEqual(publication._read_source(self.record(first)), self.raw)
        self.assertEqual(len(list(queue_manager.read_queue_files())), 1)

    def test_interrupted_delivery_needs_headroom_but_exact_metadata_replay_does_not(self):
        first = self.publish()
        record = self.record(first)
        first.queue_path.unlink()
        with mock.patch.object(source_store, "_free_bytes", return_value=0), \
                mock.patch.object(publication, "_protect_source"):
            with self.assertRaises(ValueError):
                self.publish()
            publication._create_metadata(publication._receipt(first.publication_id), publication._json(record))
            self.assertEqual(publication._read_source(self.record(first)), self.raw)
        self.assertFalse(first.queue_path.exists())

    def test_legacy_revision_coexists_with_new_chunked_revision_and_old_bytes_survive(self):
        small = row(0, text="Small original public evidence")
        self.path.write_bytes(small)
        legacy = self.publish(small)
        self.assertEqual(self.record(legacy)["version"], 1)
        self.path.write_bytes(self.raw)
        chunked = self.publish()
        self.assertEqual(self.record(chunked)["version"], 2)
        self.assertEqual(legacy.snapshot_path.read_bytes(), small)
        state = session_tracker.update_session_from_events("publicsession",
            self.events(legacy) + self.events(chunked))
        self.assertEqual(state.import_binding["publication_id"], chunked.publication_id)
        self.assertEqual(len(state.processed_event_ids), 4)

    def test_old_revisions_survive_nonappend_edits_and_truncation(self):
        first = self.publish()
        changed = self.raw.replace(b"Public synthetic", b"Edited synthetic", 1)
        self.path.write_bytes(changed)
        second = self.publish(changed)
        shortened = changed[:changed.rfind(b"\n", 0, len(changed) // 2) + 1]
        self.path.write_bytes(shortened)
        third = self.publish(shortened)
        for result, raw in ((first, self.raw), (second, changed), (third, shortened)):
            self.assertEqual(publication._read_source(self.record(result)), raw)


if __name__ == "__main__":
    unittest.main()
