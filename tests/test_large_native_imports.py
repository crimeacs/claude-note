"""Oversized native capture uses synthetic media, never personal sessions."""

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='large-native-config-')
os.environ.setdefault('CLAUDE_NOTE_VAULT_ROOT', str(Path(_BOOTSTRAP.name) / 'vault'))

from claude_note import config, importers, import_publication, models, queue_manager, session_tracker, transcript_reader


class LargeNativeImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='large-native-import-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for patcher in [mock.patch.multiple(config, STATE_DIR=self.root/'state', QUEUE_DIR=self.root/'queue'),
                mock.patch.multiple(importers, CODEX_SESSIONS=self.root/'codex', CLAUDE_PROJECTS=self.root/'claude'),
                mock.patch.object(transcript_reader, 'MAX_TRANSCRIPT_BYTES', 8192)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def native(self, source):
        media = 'A' * 20000
        if source == 'codex':
            path = importers.CODEX_SESSIONS/'2026/10/14/rollout-large.jsonl'
            rows = [
                {'type': 'session_meta', 'payload': {'id': 'large-codex', 'cwd': '/synthetic', 'source': 'vscode'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': 'Check the screenshot'}]}},
                {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'inspect', 'call_id': 'call', 'arguments': '{}'}},
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'result': media}}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call',
                    'output': [{'type': 'image', 'image_url': 'data:image/png;base64,' + media},
                               {'type': 'text', 'text': 'Exact textual tool result'}]}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'Verified the screenshot'}]}},
            ]
        else:
            path = importers.CLAUDE_PROJECTS/'synthetic/large-claude.jsonl'
            rows = [
                {'type': 'user', 'sessionId': 'large-claude', 'cwd': '/synthetic', 'entrypoint': 'cli',
                    'message': {'content': 'Check the screenshot'}},
                {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'call', 'name': 'Read', 'input': {}}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': media}},
                    {'type': 'text', 'text': 'Exact textual tool result'}]}]}, 'toolUseResult': {'file': {'base64': media}}},
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Verified the screenshot'}]}},
            ]
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b''.join(json.dumps(row).encode() + b'\n' for row in rows)
        path.write_bytes(raw)
        stamp = time.time() - 3600
        os.utime(path, (stamp, stamp))
        return path, raw

    def sweep(self, source, seen, problems):
        return (importers.sweep_codex if source == 'codex' else importers.sweep_claude_code)(seen, 0, problems=problems)

    def test_projection_keeps_text_and_original_bindings_without_copying_binary(self):
        for source in ('codex', 'claude-code'):
            with self.subTest(source=source):
                path, original = self.native(source)
                raw = transcript_reader.capture_native_transcript(path)
                self.assertLess(len(raw), 8192)
                self.assertEqual(path.read_bytes(), original)
                self.assertNotIn(b'A'*100, raw)
                content = transcript_reader.read_transcript(path, captured_bytes=raw)
                self.assertEqual(content.user_prompts, ['Check the screenshot'])
                self.assertEqual(content.assistant_texts, ['Verified the screenshot'])
                self.assertEqual(content.tool_uses[0].output, 'Exact textual tool result')
                manifest = content.coverage['native_projection']
                self.assertEqual(manifest['revision'], hashlib.sha256(original).hexdigest())
                self.assertEqual(manifest['bytes'], len(original))
                self.assertEqual(manifest['records'], len(original.splitlines()))
                for event, line in zip(content.source_events, original.splitlines()):
                    self.assertEqual(event.origin_source, str(path))
                    self.assertEqual(event.origin_revision, manifest['revision'])
                    self.assertEqual(event.origin_payload_digest, hashlib.sha256(line).hexdigest())

    def test_projection_publication_uses_original_revision_and_retries_without_duplicate(self):
        for source in ('codex', 'claude-code'):
            with self.subTest(source=source):
                path, original = self.native(source)
                seen, problems = {}, []
                self.assertEqual(self.sweep(source, seen, problems), 1)
                self.assertEqual(problems, [])
                events = [event for event in queue_manager.read_all_events() if event.session_id == 'large-' + ('codex' if source == 'codex' else 'claude')]
                self.assertEqual(len(events), 2)
                record = import_publication.validate_event(events[0])
                self.assertTrue(record['normalized'])
                self.assertEqual(record['logical_revision'], hashlib.sha256(original).hexdigest())
                state = session_tracker.update_session_from_events(events[0].session_id, events)
                content = transcript_reader.read_transcript_from_state(state)
                self.assertEqual(content.source_events[-1].origin_line, len(original.splitlines()))
                self.assertEqual(content.source_events[-1].origin_revision, hashlib.sha256(original).hexdigest())
                self.assertEqual(self.sweep(source, {}, []), 0)
                self.assertEqual(path.read_bytes(), original)

    def test_incomplete_or_malformed_oversized_sources_never_publish_or_advance_seen(self):
        for source in ('codex', 'claude-code'):
            for ending in (b'not-json\n', b'{"incomplete":'):
                with self.subTest(source=source, ending=ending):
                    path, original = self.native(source)
                    path.write_bytes(original + ending)
                    stamp = time.time() - 3600
                    os.utime(path, (stamp, stamp))
                    seen, problems = {}, []
                    self.assertEqual(self.sweep(source, seen, problems), 0)
                    self.assertEqual(seen, {})
                    self.assertEqual(len(problems), 1)
        self.assertEqual(list(queue_manager.read_all_events()), [])

    def test_oversized_hook_state_is_rebound_to_projection_once(self):
        for source in ('codex', 'claude-code'):
            with self.subTest(source=source):
                path, original = self.native(source)
                sid = 'large-' + ('codex' if source == 'codex' else 'claude')
                old = models.SessionState(session_id=sid, transcript_path=str(path),
                    first_event_ts='2026-01-01T00:00:00Z', last_event_ts='2026-01-01T00:00:01Z',
                    processed_event_ids=['hook-event'], transcript_checkpoint={'bytes': 123, 'lines': 1, 'revision': 'old'})
                session_tracker.save_session_state(old)
                self.assertEqual(self.sweep(source, {}, []), 1)
                events = [event for event in queue_manager.read_all_events() if event.session_id == sid]
                state = session_tracker.update_session_from_events(sid, events)
                self.assertIn('hook-event', state.processed_event_ids)
                self.assertTrue(state.import_binding)
                content = transcript_reader.read_transcript_from_state(state)
                self.assertEqual(content.coverage['native_projection']['revision'], hashlib.sha256(original).hexdigest())
                self.assertEqual(content.coverage['checkpoint'], 'source-changed-full-replay')
                session_tracker.save_session_state(state)
                self.assertEqual(self.sweep(source, {}, []), 0)
                self.assertEqual(len([e for e in queue_manager.read_all_events() if e.session_id == sid]), 2)

    def test_duplicate_fields_in_discarded_bookkeeping_are_rejected(self):
        path, original = self.native('codex')
        path.write_bytes(original + b'{"type":"event_msg","discarded":{"private":true,"private":false}}\n')
        with self.assertRaisesRegex(ValueError, 'malformed or incomplete'):
            transcript_reader.capture_native_transcript(path)

    def test_limits_reject_huge_records_and_text_that_cannot_fit_projection(self):
        path, original = self.native('codex')
        with mock.patch.object(transcript_reader, 'MAX_NATIVE_RECORD_BYTES', 1000):
            with self.assertRaisesRegex(ValueError, 'Transcript exceeds'):
                transcript_reader.capture_native_transcript(path)
        with mock.patch.object(transcript_reader, 'MAX_NATIVE_SOURCE_BYTES', len(original) - 1):
            with self.assertRaisesRegex(ValueError, 'Transcript exceeds'):
                transcript_reader.capture_native_transcript(path)
        rows = original.splitlines()
        rows[-1] = json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'T'*20000}]}}).encode()
        path.write_bytes(b'\n'.join(rows) + b'\n')
        with self.assertRaisesRegex(ValueError, 'Transcript exceeds'):
            transcript_reader.capture_native_transcript(path)

    def test_source_mutation_during_projection_is_rejected(self):
        path, original = self.native('codex')
        project = transcript_reader._project_native_media
        changed = False
        def mutate(value):
            nonlocal changed
            if not changed:
                with path.open('ab') as handle:
                    handle.write(b'{"type":"event_msg"}\n')
                changed = True
            return project(value)
        with mock.patch.object(transcript_reader, '_project_native_media', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during capture'):
                transcript_reader.capture_native_transcript(path)
        self.assertNotEqual(path.read_bytes(), original)

    def test_binary_projection_does_not_strip_authored_data_url_text(self):
        value = {'type': 'text', 'text': 'data:image/png;base64,authored-example'}
        self.assertEqual(transcript_reader._project_native_media(value), value)
        authored = {'type': 'base64', 'data': 'authored-example'}
        self.assertEqual(transcript_reader._project_native_media(authored), authored)


if __name__ == '__main__':
    unittest.main()
