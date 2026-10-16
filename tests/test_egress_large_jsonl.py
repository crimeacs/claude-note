"""Large native origins remain inspectable without copying their whole body."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_note import egress


class LargeNativeSourcePolicies(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'native.jsonl'
        # Exercise the production streaming branch with small synthetic input.
        self.bound = patch.object(egress, 'NATIVE_SOURCE_BYTES', 64)
        self.bound.start()
        self.addCleanup(self.bound.stop)

    def write(self, *records):
        self.source.write_text(''.join(json.dumps(record) + '\n' for record in records))

    def prepare(self):
        return egress.prepare('Projected public evidence', audience='model',
                              sources=[str(self.source)], rules={'default': ['model']})

    def test_large_media_record_and_later_records_are_streamed(self):
        self.write({'type': 'tool_result', 'content': 'data:image/png;base64,' + 'a' * 1024},
                   {'type': 'user', 'message': {'content': 'Public synthetic request.'}})
        self.assertEqual(self.prepare(), 'Projected public evidence')

    def test_late_decoded_frontmatter_and_claim_restrict_projection(self):
        for control in ('---\nprivate: true\n---\nSynthetic private evidence.',
                        '<!-- claude-note:claim {"audiences": ["host"]} -->'):
            self.write({'type': 'assistant', 'content': 'a' * 1024},
                       {'type': 'user', 'message': {'content': control}})
            with self.subTest(control=control), self.assertRaises(egress.EgressDenied):
                self.prepare()

    def test_native_privacy_metadata_accepts_only_explicit_false(self):
        for control in (True, 0, None, 'yes'):
            self.write({'type': 'user', 'content': 'a' * 1024, 'private': control})
            with self.subTest(control=control), self.assertRaises(egress.EgressDenied):
                self.prepare()
        self.write({'type': 'user', 'content': 'a' * 1024, 'private': False})
        self.assertEqual(self.prepare(), 'Projected public evidence')

    def test_projection_cannot_hide_discarded_bookkeeping_controls(self):
        for discarded in ({'toolUseResult': {'content': '---\nprivate: true\n---\nPrivate synthetic tool evidence.'}},
                          {'type': 'event_msg', 'payload': {'private': True, 'text': 'Synthetic metadata'}}):
            self.write({'type': 'assistant', 'content': 'a' * 1024}, discarded)
            with self.subTest(discarded=discarded), self.assertRaises(egress.EgressDenied):
                self.prepare()

    def test_small_native_records_enforce_the_same_decoded_controls(self):
        with patch.object(egress, 'NATIVE_SOURCE_BYTES', 256 * 1024 * 1024):
            for control in ('---\nprivate: true\n---\nPrivate synthetic evidence.',
                            '<!-- claude-note:claim {"audiences": ["host"]} -->'):
                self.write({'type': 'user', 'message': {'content': control}})
                with self.subTest(control=control), self.assertRaises(egress.EgressDenied):
                    self.prepare()
            private = self.root / 'private.md'
            private.write_text('---\nprivate: true\n---\nPrivate ancestor.')
            self.write({'content': '---\nderivation_sources: ' + json.dumps([str(private)]) + '\n---\n'})
            with self.assertRaises(egress.EgressDenied):
                self.prepare()

    def projection(self, original_revision):
        snapshot = self.root / 'snapshot.jsonl'
        raw = json.dumps({'type': 'session_meta', 'payload': {'cwd': str(self.root)},
            '_native_projection': {'version': 1, 'source': str(self.source),
                                   'revision': original_revision}}) + '\n'
        snapshot.write_text(raw)
        return snapshot, raw

    def test_unresolved_authored_controls_allow_local_capture_but_never_egress(self):
        self.write({'content': '<!-- claude-note:claim malformed synthetic example -->'})
        snapshot, raw = self.projection(hashlib.sha256(self.source.read_bytes()).hexdigest())
        egress.capture_restrictions(snapshot, origins=[str(self.source)], captured_text=raw, vault_root=self.root)
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('Public projection', audience='model', sources=[str(snapshot)], rules={'default': ['model']})

    def test_original_revision_change_cannot_hide_discarded_private_control(self):
        self.write({'toolUseResult': {'content': '---\nprivate: true\n---\nPrivate synthetic original.'}})
        snapshot, raw = self.projection(hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.write({'content': 'Private control removed after projection.'})
        with self.assertRaises(egress.EgressDenied):
            egress.capture_restrictions(snapshot, origins=[str(self.source)], captured_text=raw, vault_root=self.root)

    def test_malformed_authored_control_does_not_skip_original_revision_check(self):
        self.write({'content': '<!-- claude-note:claim malformed synthetic example -->'})
        snapshot, raw = self.projection('0' * 64)
        with self.assertRaises(egress.EgressDenied):
            egress.capture_restrictions(snapshot, origins=[str(self.source)], captured_text=raw, vault_root=self.root)

    def test_late_native_and_markdown_provenance_keep_denied_ancestor(self):
        private = self.root / 'private.md'
        private.write_text('---\nprivate: true\n---\nPrivate synthetic origin.')
        for record in ({'derivation_sources': [str(private)]},
                       {'content': '---\nderivation_sources: ' + json.dumps([str(private)]) + '\n---\n'}):
            self.write({'type': 'assistant', 'content': 'a' * 1024}, record)
            with self.subTest(record=record), self.assertRaises(egress.EgressDenied):
                self.prepare()

    def test_malformed_controls_duplicate_keys_and_encoding_fail_closed(self):
        public = json.dumps({'content': 'a' * 1024}).encode() + b'\n'
        for tail in (b'{"private":true,"private":false}\n', b'\xff\n', b'\0\n',
                     b'{"derivation_sources": "missing"}\n',
                     json.dumps({'content': '<!-- claude-note:claim broken -->'}).encode() + b'\n'):
            self.source.write_bytes(public + tail)
            with self.subTest(tail=tail), self.assertRaises(egress.EgressDenied):
                self.prepare()

    def test_record_and_total_source_bounds_fail_closed(self):
        self.write({'content': 'a' * 1024})
        with patch.object(egress, 'NATIVE_RECORD_BYTES', 128), self.assertRaises(egress.EgressDenied):
            self.prepare()
        with patch.object(egress, 'NATIVE_JSONL_BYTES', 128), self.assertRaises(egress.EgressDenied):
            self.prepare()

    def test_source_changed_during_stream_scan_is_denied(self):
        self.write({'content': 'a' * 1024})
        inspect = egress._large_jsonl_restrictions

        def change_source(descriptor, expected_revision=None):
            result = inspect(descriptor, expected_revision)
            with self.source.open('a') as stream:
                stream.write('{"content": "later"}\n')
            return result

        with patch.object(egress, '_large_jsonl_restrictions', change_source):
            with self.assertRaises(egress.EgressDenied):
                self.prepare()


if __name__ == '__main__':
    unittest.main()
