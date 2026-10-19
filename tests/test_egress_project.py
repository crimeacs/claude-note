"""Project grants cannot authorize neighboring or malformed native sources."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from claude_note import egress


class CodexProjectAudienceRules(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'Sweat'
        self.project.mkdir()
        self.store = self.root / 'sessions'
        self.store.mkdir()
        self.source = self.store / 'rollout.jsonl'
        self.rules = {'default': ['local', 'host'], 'rules': [
            {'source': str(self.store / '*'), 'project': str(self.project),
             'native': 'codex',
             'audiences': ['local', 'host', 'model', 'staging']}]}

    def write(self, cwd):
        self.source.write_text(json.dumps({'type': 'session_meta', 'payload': {'cwd': cwd}}) + '\n')

    def allowed(self):
        return egress.audiences(str(self.source), rules=self.rules)

    def test_matching_project_and_descendant_are_granted(self):
        for cwd in (self.project, self.project / 'frontend', self.project / 'nested' / 'work'):
            self.write(str(cwd))
            with self.subTest(cwd=cwd):
                self.assertIn('model', self.allowed())
                self.assertIn('staging', self.allowed())

    def test_foreign_sibling_and_relative_cwd_are_not_granted(self):
        for cwd in (self.root / 'Personal', self.root / 'Sweat-copy', 'Sweat',
                    self.project / '..' / 'Personal'):
            self.write(str(cwd))
            with self.subTest(cwd=cwd):
                self.assertNotIn('model', self.allowed())

    def test_missing_invalid_duplicate_or_later_metadata_are_not_granted(self):
        for body in ('{}\n', '{broken}\n',
                     '{"type":"session_meta","payload":{"cwd":' + json.dumps(str(self.project)) + ',"cwd":"/elsewhere"}}\n',
                     json.dumps({'type': 'user', 'payload': {'cwd': str(self.project)}}) + '\n',
                     '{}\n' + json.dumps({'type': 'session_meta', 'payload': {'cwd': str(self.project)}}) + '\n',
                     json.dumps({'type': 'session_meta', 'payload': {'cwd': str(self.project), 'extra': 'x' * (1024 * 1024)}}) + '\n'):
            self.source.write_text(body)
            with self.subTest(body=body[:70]):
                self.assertNotIn('model', self.allowed())
        self.source.write_bytes(b'\xff\n')
        self.assertNotIn('model', self.allowed())

    def test_source_symlinks_hardlinks_and_absent_sources_are_not_granted(self):
        self.write(str(self.project))
        original = self.store / 'original.jsonl'
        self.source.rename(original)
        self.source.symlink_to(original)
        self.assertNotIn('model', self.allowed())
        self.source.unlink()
        os.link(original, self.source)
        self.assertNotIn('model', self.allowed())
        self.source.unlink()
        self.assertNotIn('model', self.allowed())

    def test_cwd_alias_outside_project_is_not_granted(self):
        foreign = self.root / 'Personal'
        foreign.mkdir()
        alias = self.project / 'foreign-alias'
        alias.symlink_to(foreign, target_is_directory=True)
        self.write(str(alias))
        self.assertNotIn('model', self.allowed())

    def test_whole_source_controls_still_restrict_matching_project(self):
        self.write(str(self.project))
        with self.source.open('a') as stream:
            stream.write('<!-- claude-note:claim {"audiences": ["host"]} -->\n')
        self.assertIn('model', self.allowed())
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('Public projection', audience='model', sources=[str(self.source)], rules=self.rules)

    def note_rule(self, *anchors):
        return {'default': ['local', 'host'], 'rules': [
            {'source': str(self.root / 'note*.md'), 'project': str(self.project),
             'project_sources': list(anchors), 'audiences': ['model', 'staging']}]}

    def note(self, name, origins):
        path = self.root / name
        path.write_text('---\nderivation_sources: ' + json.dumps([str(item) for item in origins]) + '\n---\nPublic synthetic note.')
        return path

    def test_note_grant_requires_complete_project_ancestry(self):
        self.write(str(self.project))
        note = self.note('note.md', [self.project, self.source])
        self.assertIn('staging', egress.audiences(str(note), rules=self.note_rule()))
        foreign = self.root / 'personal.md'
        foreign.write_text('Unrelated personal synthetic source.')
        for origins in ([], [foreign], [self.project, foreign], [self.root / 'missing.md']):
            self.note('note.md', origins)
            with self.subTest(origins=origins):
                self.assertNotIn('staging', egress.audiences(str(note), rules=self.note_rule()))

    def test_note_ancestry_cycles_are_not_project_evidence(self):
        one = self.note('note-one.md', [self.root / 'note-two.md'])
        self.note('note-two.md', [one, self.project])
        self.assertNotIn('staging', egress.audiences(str(one), rules=self.note_rule()))

    def test_foreign_native_cwd_cannot_borrow_project_provenance(self):
        self.write(str(self.root / 'Personal'))
        with self.source.open('a') as stream:
            stream.write(json.dumps({'derivation_sources': [str(self.project)]}) + '\n')
        self.assertNotIn('model', self.allowed())
        note = self.note('note.md', [self.project, self.source])
        self.assertNotIn('staging', egress.audiences(str(note), rules=self.note_rule()))

    def test_explicit_project_sources_do_not_grant_neighboring_projects(self):
        selected = self.root / 'claude-project-Sweat'
        selected.mkdir()
        origin = selected / 'native.jsonl'
        origin.write_text('{"type":"user","message":"Synthetic source"}\n')
        note = self.note('note.md', [origin])
        rules = self.note_rule(str(selected), str(selected / '*'))
        self.assertIn('staging', egress.audiences(str(note), rules=rules))
        other = self.root / 'claude-project-Personal.jsonl'
        other.write_text(origin.read_text())
        self.note('note.md', [other])
        self.assertNotIn('staging', egress.audiences(str(note), rules=rules))

    def test_snapshot_protected_lineage_is_project_evidence_but_unknown_is_not(self):
        self.write(str(self.project))
        snapshot = self.root / 'snapshot.jsonl'
        snapshot.write_text('{"type":"user","message":"Synthetic projection"}\n')
        ancestry = '---\nderivation_sources: ' + json.dumps([str(self.source)]) + '\n---\n'
        egress.record_derivation(snapshot, ancestry, vault_root=self.root)
        note = self.note('note.md', [snapshot])
        self.assertIn('staging', egress.audiences(str(note), rules=self.note_rule()))
        egress.record_derivation(snapshot, '---\nderivation_sources: malformed\n---\n', vault_root=self.root)
        self.assertNotIn('staging', egress.audiences(str(note), rules=self.note_rule()))


if __name__ == '__main__':
    unittest.main()
