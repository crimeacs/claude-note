"""Artifact audience restrictions with synthetic files and no external calls."""

import json
import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from claude_note import egress, mutation, state_io
from claude_note.verified_claims import Claim


class ClaimAudienceEgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='claude-note-claim-egress-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.public = self.root / 'public.md'
        self.public.write_text('Public synthetic source evidence.')
        self.rules = {'default': ['local', 'host', 'model', 'staging']}

    def marker(self, audiences=('host',), **fields):
        return Claim('synthetic', 'Synthetic claim facts.', audiences=list(audiences),
                     derivation_sources=[str(self.public)], **fields).marker()

    def prepare(self, text, sources=None, audience='model', **kwargs):
        return egress.prepare(text, audience=audience, sources=sources or [str(self.public)],
                              rules=self.rules, **kwargs)

    def test_host_only_marker_denies_assembled_text_even_with_explicit_source_grant(self):
        for audience in ('model', 'staging'):
            for explicit in (False, True):
                with self.subTest(audience=audience, explicit=explicit), self.assertRaises(egress.EgressDenied):
                    self.prepare(self.marker(), audience=audience, explicit=explicit)
        self.assertEqual(self.prepare(self.marker(), audience='host'), self.marker())

    def test_host_only_source_claim_denies_prompt_without_visible_marker(self):
        path = self.root / 'claim.md'
        path.write_text(self.marker())
        for audience in ('model', 'staging'):
            with self.subTest(audience=audience), self.assertRaises(egress.EgressDenied):
                self.prepare('Wrapped synthetic facts.', [str(path)], audience)

    def test_transitive_source_claim_restricts_derived_prompt(self):
        claim, derived = self.root / 'claim.md', self.root / 'derived.md'
        claim.write_text(self.marker())
        derived.write_text('---\nderivation_sources: ' + json.dumps([str(claim)]) + '\n---\nDerived facts.')
        with self.assertRaises(egress.EgressDenied):
            self.prepare('Wrapped facts.', [str(derived)])

    def test_all_accepted_claim_formats_preserve_private_origins(self):
        private = self.root / 'private.md'
        private.write_text('---\n"private": true\n---\nSynthetic private facts.')
        claim = Claim('synthetic', 'Derived synthetic facts.', audiences=['model'],
                      derivation_sources=[str(private)])
        markers = [claim.marker(),
                   '<!-- claude-note:claim\n' + json.dumps(asdict(claim), indent=2) + '\n-->',
                   '  <!--   claude-note:claim  ' + json.dumps(asdict(claim)) + '   -->']
        path = self.root / 'claim.md'
        for marker in markers:
            with self.subTest(marker=marker[:50]):
                self.assertEqual(egress.derivations(marker), [str(private)])
                path.write_text(marker)
                for payload, sources in ((marker, [str(self.public)]), ('Wrapped facts.', [str(path)])):
                    with self.assertRaises(egress.EgressDenied):
                        self.prepare(payload, sources)

    def test_missing_claim_audience_defaults_host_and_malformed_audiences_fail_closed(self):
        record = {'claim_id': 'legacy', 'text': 'Synthetic facts.'}
        values = [None, 'model', {'model': True}, ['model', 1], ['model', ''], [' model'], ['model'] * 201]
        markers = ['<!-- claude-note:claim ' + json.dumps(record) + ' -->']
        markers += ['<!-- claude-note:claim ' + json.dumps({**record, 'audiences': value}) + ' -->'
                    for value in values]
        markers += ['<!-- claude-note:claim {"audiences":["host"],"audiences":["model"]} -->',
                    '<!-- claude-note:claim {not-json} -->', '<!-- claude-note:claim {"audiences":["model"]}']
        for marker in markers:
            with self.subTest(marker=marker[:100]), self.assertRaises(egress.EgressDenied):
                self.prepare(marker)

    def test_multiple_claims_intersect_and_plain_prose_stays_policy_governed(self):
        marker = self.marker(('host', 'model'))
        self.assertEqual(self.prepare(marker), marker)
        with self.assertRaises(egress.EgressDenied):
            self.prepare(marker + '\n' + self.marker(('host', 'staging')))
        self.assertEqual(self.prepare('Legacy plain prose.'), 'Legacy plain prose.')

    def test_marker_restriction_survives_removal_and_authored_broadening(self):
        path = self.root / 'claim.md'
        mutation.create_note(path, self.marker(), vault_root=self.root, operation_id='create')
        path.write_text('Synthetic facts with the authored marker removed.')
        with self.assertRaises(egress.EgressDenied):
            self.prepare(path.read_text(), [str(path)])
        # Later content can narrow the ledger, but cannot broaden host-only.
        egress.record_derivation(path, self.marker(('host', 'model', 'staging')), vault_root=self.root)
        with self.assertRaises(egress.EgressDenied):
            self.prepare(path.read_text(), [str(path)])
        self.assertEqual(egress.recorded_derivations(path), [str(self.public)])

    def test_nested_ledger_cannot_shadow_outer_artifact_restriction(self):
        nested = self.root / 'nested'
        nested.mkdir()
        path = nested / 'claim.md'
        path.write_text('Synthetic facts without metadata.')
        egress.record_derivation(path, self.marker(('host', 'model')), vault_root=self.root)
        egress.record_derivation(path, self.marker(('host', 'staging')), vault_root=nested)
        for audience in ('model', 'staging'):
            with self.subTest(audience=audience), self.assertRaises(egress.EgressDenied):
                self.prepare(path.read_text(), [str(path)], audience)
        # Empty newer metadata cannot clear either ancestor's restriction.
        egress.record_derivation(path, 'Ordinary prose.', vault_root=nested)
        with self.assertRaises(egress.EgressDenied):
            self.prepare(path.read_text(), [str(path)])

    def test_malformed_audience_ledger_needs_owner_reconciliation(self):
        path = self.root / 'claim.md'
        path.write_text('Synthetic facts.')
        ledger = egress._lineage_path(path, self.root)
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({'version': 1, 'origins': [], 'artifact_audiences': 'model'}))
        with self.assertRaises(egress.EgressDenied):
            self.prepare(path.read_text(), [str(path)])
        before = ledger.read_bytes()
        with self.assertRaises(egress.EgressDenied):
            egress.record_derivation(path, self.marker(('model',)), vault_root=self.root)
        self.assertEqual(ledger.read_bytes(), before)

    def test_legacy_control_is_recorded_before_first_unattended_replacement(self):
        for name, before in (('claim', self.marker()),
                             ('private', '---\n"private": true\n---\nSynthetic confidential facts.')):
            with self.subTest(control=name):
                path = self.root / (name + '.md')
                path.write_text(before)
                # Model an existing pre-2.1.2 lineage record without audiences.
                egress.record_derivation(path, '---\nderivation_sources: ' + json.dumps([str(self.public)]) + '\n---\n',
                                         vault_root=self.root)
                result = mutation.mutate(path, lambda old: 'Replacement without controls.',
                    vault_root=self.root, expected_revision=mutation.revision(before), machine_owned=True,
                    operation_id='replace-' + name)
                self.assertTrue(result.applied)
                self.assertNotIn('claude-note:claim', path.read_text())
                with self.assertRaises(egress.EgressDenied):
                    self.prepare(path.read_text(), [str(path)])

    def test_old_origin_only_ledger_remains_compatible(self):
        path = self.root / 'plain.md'
        path.write_text('Legacy plain prose.')
        ledger = egress._lineage_path(path, self.root)
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({'version': 1, 'origins': [str(self.public)], 'unknown': False}))
        self.assertEqual(egress.recorded_derivations(path), [str(self.public)])
        self.assertEqual(self.prepare(path.read_text(), [str(path)]), path.read_text())

    def test_claim_control_count_and_size_bounds_fail_closed(self):
        for text in (self.marker(('model',)) * 2001,
                     '<!-- claude-note:claim ' + 'x' * (mutation.MAX_NOTE_BYTES + 1) + ' -->'):
            with self.assertRaises(egress.EgressDenied):
                self.prepare(text)
        marker = self.marker(('model',))
        with mock.patch.object(mutation, 'MAX_NOTE_BYTES', len(marker.encode()) * 2), self.assertRaises(egress.EgressDenied):
            self.prepare(marker * 3)

    def test_concurrent_direct_writers_cannot_restore_a_denied_audience(self):
        path = self.root / 'claim.md'
        path.write_text('Synthetic facts without visible controls.')
        egress.record_derivation(path, '---\nderivation_sources: ' + json.dumps([str(self.public)]) + '\n---\n',
                                 vault_root=self.root)
        ledger = egress._lineage_path(path, self.root)
        first_read, second_read = threading.Event(), threading.Event()
        release_first, release_second = threading.Event(), threading.Event()
        failures = []
        read_json = state_io.read_json
        def read(target):
            value = read_json(target)
            if target == ledger:
                signal, release = ((first_read, release_first) if threading.current_thread().name == 'host-writer'
                                   else (second_read, release_second))
                signal.set()
                if not release.wait(3):
                    raise TimeoutError('Synthetic reader gate expired')
            return value
        def write(audience):
            try:
                egress.record_derivation(path, self.marker((audience,)), vault_root=self.root)
            except Exception as exc:
                failures.append(exc)
        first = threading.Thread(target=write, args=('host',), name='host-writer')
        second = threading.Thread(target=write, args=('model',), name='model-writer')
        with mock.patch.object(state_io, 'read_json', side_effect=read):
            try:
                first.start()
                self.assertTrue(first_read.wait(2))
                second.start()
                # On an unlocked implementation, both readers take the same
                # old ledger before host commits; model then overwrites it.
                self.assertFalse(second_read.wait(.1), 'Second writer read through a held ledger lock')
                release_first.set()
                first.join(2)
                self.assertFalse(first.is_alive())
                release_second.set()
                second.join(2)
                self.assertFalse(second.is_alive())
            finally:
                release_first.set(); release_second.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(state_io.read_json(ledger)['artifact_audiences'], [])
        with self.assertRaises(egress.EgressDenied):
            self.prepare(path.read_text(), [str(path)])

    def test_ledger_lock_is_process_shared_and_bounded(self):
        path = self.root / 'claim.md'
        path.write_text('Synthetic facts.')
        egress.record_derivation(path, self.marker(('host', 'model')), vault_root=self.root)
        ledger = egress._lineage_path(path, self.root)
        descriptor = state_io.open_lock(ledger.with_suffix('.lock'))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            before = ledger.read_bytes()
            script = '''import sys
from pathlib import Path
from claude_note import egress
egress.LINEAGE_LOCK_TIMEOUT=.1
try:
 egress.record_derivation(Path(sys.argv[1]),sys.argv[2],vault_root=Path(sys.argv[3]))
except TimeoutError:
 print('bounded lock timeout')
else:
 raise SystemExit('process did not respect ledger lock')
'''
            environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                'XDG_CONFIG_HOME': str(self.root / 'config'),
                'PYTHONPATH': str(Path(egress.__file__).resolve().parents[1])}
            process = subprocess.run([sys.executable, '-B', '-c', script, str(path), self.marker(('host',)),
                                      str(self.root)], env=environment, capture_output=True, text=True, timeout=3)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertIn('bounded lock timeout', process.stdout)
            self.assertEqual(ledger.read_bytes(), before)
        finally:
            os.close(descriptor)
        # Lock release permits a later conservative intersection.
        egress.record_derivation(path, self.marker(('host',)), vault_root=self.root)
        with self.assertRaises(egress.EgressDenied):
            self.prepare(path.read_text(), [str(path)])


if __name__ == '__main__':
    unittest.main()
