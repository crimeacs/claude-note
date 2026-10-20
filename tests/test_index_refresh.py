"""Cooperative refresh leases use synthetic metadata only, never QMD/models."""

import contextlib
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from claude_note import entry, freshness, index_refresh, mutation, setup_recall
from claude_note.index_proof import verify as verify_index


class IndexRefreshTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='claude-note-lease-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.qmd = self.root / 'qmd'
        self.qmd.mkdir()
        self.state = self.root / 'leases'
        self.cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        environment = mock.patch.dict(os.environ, {'QMD_CONFIG_DIR': str(self.qmd),
            'XDG_CONFIG_HOME': str(self.root / 'config'), 'XDG_CACHE_HOME': str(self.root / 'cache'),
            'INDEX_PATH': ''})
        environment.start()
        self.addCleanup(environment.stop)
        state = mock.patch.object(index_refresh, 'STATE_ROOT', self.state)
        state.start()
        self.addCleanup(state.stop)
        proof = mock.patch('claude_note.index_proof.verify', return_value={'lexical': True, 'vector': True, 'reason': ''})
        self.proof = proof.start()
        self.addCleanup(proof.stop)
        executable = mock.patch.object(setup_recall, 'qmd_exe', return_value='/synthetic/qmd')
        executable.start()
        self.addCleanup(executable.stop)
        self.configure()
        self.add_note('first')

    def configure(self, *, index='', pattern='**/*.md', vault=None):
        path = self.qmd / ((index or 'index') + '.yml')
        path.write_text(json.dumps({'collections': {'notes': {
            'path': str(vault or self.vault), 'pattern': pattern}}}))

    def add_note(self, name, *, vault=None):
        mutation.create_note(name + '.md', 'Synthetic ' + name, vault_root=vault or self.vault,
                             operation_id=name)

    def begin(self, index=''):
        with mock.patch.object(setup_recall.recall_mod.Settings, 'load',
                               return_value=setup_recall.recall_mod.Settings(index=index)):
            result = index_refresh.begin(index)
        self.assertEqual(result['status'], 'captured')
        self.assertFalse(result['warning'])
        return result['token']

    def generations(self, vault=None):
        snapshot = freshness.snapshot(vault or self.vault)
        return snapshot['saved_generation'], snapshot['lexical_generation'], snapshot['vector_generation']

    def test_success_only_acknowledges_captured_generations(self):
        token = self.begin()
        self.add_note('during-indexing')
        with mock.patch.object(setup_recall.subprocess, 'run', side_effect=AssertionError('No commands')):
            result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(result['status'], 'acknowledged')
        self.assertEqual(result['acknowledged'], {'lexical': 1, 'vector': 1})
        self.assertEqual(self.generations(), (2, 1, 1))
        self.assertEqual(set(freshness.snapshot(self.vault)['dirty']), {'during-indexing.md'})

    def test_stage_failure_and_lexical_only_are_distinct(self):
        failed = self.begin()
        result = index_refresh.finish(failed)
        self.assertEqual(result['status'], 'no_successful_stages')
        self.assertEqual(self.generations(), (1, 0, 0))
        token = self.begin()
        result = index_refresh.finish(token, lexical_ok=True)
        self.assertEqual(result['acknowledged'], {'lexical': 1, 'vector': 0})
        self.assertEqual(self.generations(), (1, 1, 0))
        self.assertTrue(freshness.snapshot(self.vault)['dirty'])

    def test_missing_indexed_revision_never_acknowledges_successful_exit(self):
        token = self.begin()
        self.proof.return_value = {'lexical': False, 'vector': False, 'reason': 'Indexed revision is missing or stale'}
        result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(result['acknowledged'], {'lexical': 0, 'vector': 0})
        self.assertEqual(self.generations(), (1, 0, 0))
        self.assertIn('missing', result['warning'])
        with mock.patch('claude_note.index_proof.verify', side_effect=AssertionError('Proof cannot change on replay')):
            replay = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_partial_wrong_model_or_skipped_vectors_leave_vector_dirty(self):
        token = self.begin()
        self.proof.return_value = {'lexical': True, 'vector': False, 'reason': 'Vectors incomplete for selected model'}
        result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(result['acknowledged'], {'lexical': 1, 'vector': 0})
        self.assertEqual(self.generations(), (1, 1, 0))
        self.assertIn('first.md', freshness.snapshot(self.vault)['dirty'])

    def test_inactive_or_changed_retained_revision_never_acknowledges_newer_notes(self):
        for scenario, reason in [('inactive', 'proof_revision_inactive'),
                                 ('changed', 'proof_revision_mismatch')]:
            with self.subTest(scenario=scenario):
                vault = self.root / scenario
                vault.mkdir()
                self.configure(vault=vault)
                self.add_note('retained', vault=vault)
                index_refresh.finish(self.begin(), lexical_ok=True, vector_ok=True)
                self.assertFalse(freshness.snapshot(vault)['dirty'])
                retained_revision = freshness.snapshot(vault)['retained_revisions']['retained.md']['revision']
                self.add_note('newer', vault=vault)
                database, _ = index_refresh._paths('')
                database.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(database) as db:
                    db.executescript('DROP TABLE IF EXISTS documents; DROP TABLE IF EXISTS content; '
                        'DROP TABLE IF EXISTS documents_fts; '
                        'CREATE TABLE documents(id INTEGER PRIMARY KEY,collection TEXT,path TEXT,title TEXT,hash TEXT,active INTEGER); '
                        'CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT); '
                        'CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body);')
                    for number, (path, entry) in enumerate(freshness.snapshot(vault)['retained_revisions'].items(), 1):
                        body = (vault / path).read_text()
                        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,1)',
                                   (number, 'notes', path, 'Synthetic', entry['revision']))
                        db.execute('INSERT INTO content VALUES(?,?)', (entry['revision'], body))
                        db.execute('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(?,?,?,?)',
                                   (number, 'notes/' + path, 'Synthetic', body))
                    if scenario == 'inactive':
                        (vault / 'retained.md').unlink()
                        db.execute("UPDATE documents SET active=0 WHERE path='retained.md'")
                    else:
                        changed = 'Synthetic edit outside the mutation journal'
                        changed_revision = hashlib.sha256(changed.encode()).hexdigest()
                        (vault / 'retained.md').write_text(changed)
                        db.execute('INSERT INTO content VALUES(?,?)', (changed_revision, changed))
                        db.execute("UPDATE documents SET hash=? WHERE path='retained.md'", (changed_revision,))
                        db.execute("UPDATE documents_fts SET body=? WHERE filepath='notes/retained.md'", (changed,))
                token = self.begin()
                with mock.patch('claude_note.index_proof.verify', wraps=verify_index) as proof, \
                        mock.patch.object(setup_recall, 'qmd_sdk', return_value=None):
                    result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
                    replay = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
                self.assertEqual(proof.call_count, 1)
                self.assertEqual(len(proof.call_args.kwargs['references']), 2)
                self.assertEqual(result['acknowledged'], {'lexical': 0, 'vector': 0})
                self.assertEqual(result['warning'], reason)
                self.assertTrue(replay['replayed'])
                self.assertEqual(replay['acknowledged'], result['acknowledged'])
                self.assertEqual(self.generations(vault), (2, 1, 1))
                current = freshness.snapshot(vault)
                self.assertEqual(set(current['dirty']), {'newer.md'})
                self.assertEqual(current['retained_revisions']['retained.md']['revision'], retained_revision)

    def test_expiry_during_proof_has_no_acknowledgement_effect(self):
        token = self.begin()
        lease = index_refresh._read(token)
        clock = [lease.created_at + 1]
        def proof(**kwargs):
            clock[0] = lease.expires_at
            return {'lexical': True, 'vector': True, 'reason': ''}
        with mock.patch.object(index_refresh.time, 'time', side_effect=lambda: clock[0]), \
             mock.patch('claude_note.index_proof.verify', side_effect=proof):
            with self.assertRaisesRegex(ValueError, 'expired'):
                index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))
        self.assertEqual(index_refresh._read(token).state, 'open')

    def test_canonical_index_change_during_proof_has_no_acknowledgement_effect(self):
        token = self.begin()
        selected = setup_recall.recall_mod.Settings()
        def proof(**kwargs):
            selected.index = 'changed'
            return {'lexical': True, 'vector': True, 'reason': ''}
        with mock.patch.object(setup_recall.recall_mod.Settings, 'load', return_value=selected), \
             mock.patch('claude_note.index_proof.verify', side_effect=proof):
            with self.assertRaisesRegex(ValueError, 'Canonical recall index changed'):
                index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))
        self.assertEqual(index_refresh._read(token).state, 'open')

    def test_expiry_between_acknowledgements_never_advances_the_later_stage(self):
        token = self.begin()
        lease = index_refresh._read(token)
        clock = [lease.created_at + 1]
        original = freshness.acknowledge
        def acknowledge(generation, **kwargs):
            result = original(generation, **kwargs)
            clock[0] = lease.expires_at
            return result
        with mock.patch.object(index_refresh.time, 'time', side_effect=lambda: clock[0]), \
             mock.patch.object(freshness, 'acknowledge', side_effect=acknowledge):
            with self.assertRaisesRegex(ValueError, 'expired'):
                index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 1, 0))
        self.assertEqual(index_refresh._read(token).state, 'finishing')
        self.assertIn('first.md', freshness.snapshot(self.vault)['dirty'])

    def test_captured_references_are_frozen_and_bound_to_written_revision(self):
        token = self.begin()
        ref = index_refresh._read(token).captured[0].references[0]
        self.assertEqual((ref.collection, ref.path), ('notes', 'first.md'))
        self.assertEqual(ref.revision, freshness.snapshot(self.vault)['dirty']['first.md']['revision'])
        with self.assertRaises(FrozenInstanceError):
            ref.revision = '0' * 64
        self.add_note('later')
        index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.proof.call_count, 1)
        self.assertEqual(self.proof.call_args.kwargs['references'], ({'collection': ref.collection,
                          'path': ref.path, 'revision': ref.revision},))

    def test_noncanonical_named_index_cannot_claim_default_journal(self):
        self.configure(index='other')
        result = index_refresh.begin('other')
        self.assertEqual(result['status'], 'unbound')
        self.assertIn('canonical', result['warning'])
        index_refresh.finish(result['token'], lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_another_configured_index_cannot_rebind_an_owned_journal(self):
        token = self.begin()
        index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.add_note('new')
        self.configure(index='other')
        with mock.patch.object(setup_recall.recall_mod.Settings, 'load',
                               return_value=setup_recall.recall_mod.Settings(index='other')):
            result = index_refresh.begin('other')
            self.assertEqual(result['status'], 'unbound')
            self.assertIn('another index', result['warning'])
            index_refresh.finish(result['token'], lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (2, 1, 1))
        self.assertIn('new.md', freshness.snapshot(self.vault)['dirty'])

    def test_complete_receipt_is_idempotent_and_rejects_different_flags(self):
        token = self.begin()
        index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        body = index_refresh._path(token).read_bytes()
        with mock.patch.object(freshness, 'acknowledge', side_effect=AssertionError('Duplicate ack')):
            result = index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertTrue(result['replayed'])
        self.assertEqual(index_refresh._path(token).read_bytes(), body)
        with self.assertRaisesRegex(ValueError, 'flags differ'):
            index_refresh.finish(token, lexical_ok=True)

    def test_crash_after_partial_ack_preserves_intent_and_replays_same_flags(self):
        token = self.begin()
        original = freshness.acknowledge
        def fail_vector(generation, **kwargs):
            if kwargs['capability'] == 'vector':
                raise OSError('Synthetic interrupted vector acknowledgement')
            return original(generation, **kwargs)
        with mock.patch.object(freshness, 'acknowledge', side_effect=fail_vector):
            with self.assertRaises(OSError):
                index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(index_refresh._read(token).state, 'finishing')
        self.assertEqual(self.generations(), (1, 1, 0))
        with self.assertRaisesRegex(ValueError, 'flags differ'):
            index_refresh.finish(token, lexical_ok=True)
        index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 1, 1))
        self.assertFalse(freshness.snapshot(self.vault)['dirty'])

    def test_vector_requires_lexical_and_flags_are_strict_booleans(self):
        token = self.begin()
        for flags in ({'vector_ok': True}, {'lexical_ok': 1}, {'lexical_ok': True, 'vector_ok': 1}):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                index_refresh.finish(token, **flags)
        self.assertEqual(index_refresh._read(token).state, 'open')
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_expired_or_backwards_clock_does_not_acknowledge(self):
        token = self.begin()
        lease = index_refresh._read(token)
        self.assertEqual(lease.expires_at - lease.created_at, 7200)
        for now in (lease.created_at - 1, lease.expires_at, lease.expires_at + 1):
            with self.subTest(now=now), mock.patch.object(index_refresh.time, 'time', return_value=now):
                with self.assertRaisesRegex(ValueError, 'expired|clock'):
                    index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_config_edit_or_repoint_does_not_acknowledge(self):
        token = self.begin()
        original = (self.qmd / 'index.yml').read_bytes()
        for pattern in ('*.md', 'projects/**/*.md'):
            self.configure(pattern=pattern)
            with self.assertRaisesRegex(ValueError, 'configuration|scope'):
                index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        (self.qmd / 'index.yml').write_bytes(original)
        index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 1, 1))

    def test_scope_environment_changed_after_begin_does_not_acknowledge(self):
        for key, value in (('INDEX_PATH', str(self.root / 'different.sqlite')),
                           ('XDG_CACHE_HOME', str(self.root / 'different-cache'))):
            token = self.begin()
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(ValueError, 'scope changed'):
                    index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_root_symlink_repoint_is_scope_change(self):
        other = self.root / 'other'
        other.mkdir()
        self.add_note('other', vault=other)
        alias = self.root / 'configured-vault'
        alias.symlink_to(self.vault, target_is_directory=True)
        self.configure(vault=alias)
        token = self.begin()
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'scope changed'):
            index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))
        self.assertEqual(self.generations(other), (1, 0, 0))

    def test_project_local_default_is_unbound_but_explicit_named_index_is_bound(self):
        local = self.root / '.qmd'
        local.mkdir()
        child = self.root / 'child'
        child.mkdir()
        self.configure(index='owned')
        os.chdir(child)
        for name in ('index.yaml', 'index.yml'):
            with self.subTest(name=name):
                config = local / name
                config.write_text('collections: {}')
                result = index_refresh.begin()
                self.assertEqual(result['status'], 'unbound')
                self.assertIn('Project-local', result['warning'])
                receipt = index_refresh.finish(result['token'], lexical_ok=True, vector_ok=True)
                self.assertEqual(receipt['status'], 'unbound')
                self.assertEqual(receipt['acknowledged'], {'lexical': 0, 'vector': 0})
                self.assertEqual(self.generations(), (1, 0, 0))
                token = self.begin('owned')
                self.assertEqual(index_refresh._read(token).index, 'owned')
                config.unlink()

    def test_appearing_project_local_config_after_begin_blocks_ack(self):
        token = self.begin()
        (self.root / '.qmd').mkdir()
        (self.root / '.qmd/index.yaml').write_text('collections: {}')
        with self.assertRaisesRegex(ValueError, 'scope changed'):
            index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_unsupported_database_override_or_relative_environment_is_unbound(self):
        for key, value in (('INDEX_PATH', str(self.root / 'override.sqlite')),
                           ('QMD_CONFIG_DIR', 'relative'), ('XDG_CACHE_HOME', 'relative'),
                           ('XDG_CONFIG_HOME', 'relative')):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                result = index_refresh.begin()
                self.assertEqual(result['status'], 'unbound')
                self.assertIn('no freshness will be acknowledged', result['warning'])
                index_refresh.finish(result['token'], lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_missing_or_filtered_config_warns_instead_of_claiming_freshness(self):
        for config in ('missing', 'filtered'):
            with self.subTest(config=config):
                self.configure(pattern='*.md')
                if config == 'missing':
                    (self.qmd / 'index.yml').unlink()
                result = index_refresh.begin()
                self.assertEqual(result['status'], 'unbound')
                self.assertIn('no freshness will be acknowledged', result['warning'])
                receipt = index_refresh.finish(result['token'], lexical_ok=True, vector_ok=True)
                self.assertEqual(receipt['status'], 'unbound')
                self.assertEqual(receipt['acknowledged'], {'lexical': 0, 'vector': 0})
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_xdg_config_is_used_when_qmd_config_dir_is_empty(self):
        configured = self.root / 'config/qmd'
        configured.mkdir(parents=True)
        (self.qmd / 'index.yml').replace(configured / 'index.yml')
        with mock.patch.dict(os.environ, {'QMD_CONFIG_DIR': ''}):
            token = self.begin()
            index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 1, 1))

    def test_default_home_selector_changes_are_guarded_without_home_env_mutation(self):
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': ''}), \
             mock.patch.object(setup_recall, 'HOME', self.root), \
             mock.patch.object(index_refresh, '_qmd_home', return_value=self.root) as selector:
            token = self.begin()
            selector.return_value = self.root / 'different-default-home'
            with self.assertRaisesRegex(ValueError, 'scope changed'):
                index_refresh.finish(token, lexical_ok=True, vector_ok=True)
            unbound = index_refresh.begin()
            self.assertEqual(unbound['status'], 'unbound')
            self.assertIn('default home differs', unbound['warning'])
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_explicit_config_and_cache_do_not_depend_on_default_home(self):
        with mock.patch.object(index_refresh, '_qmd_home', side_effect=AssertionError('Explicit dirs')):
            token = self.begin()
            index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 1, 1))

    def test_scope_change_during_capture_returns_unbound(self):
        original = setup_recall._freshness_capture
        def capture(index):
            result = original(index)
            os.environ['XDG_CACHE_HOME'] = str(self.root / 'changed-cache')
            return result
        with mock.patch.object(setup_recall, '_freshness_capture', side_effect=capture):
            result = index_refresh.begin()
        self.assertEqual(result['status'], 'unbound')
        self.assertIn('scope changed during capture', result['warning'])
        self.assertFalse(index_refresh._read(result['token']).captured)

    def test_lease_schema_is_strict_bounded_and_frozen(self):
        token = self.begin()
        lease = index_refresh._read(token)
        with self.assertRaises(FrozenInstanceError):
            lease.state = 'complete'
        original = json.loads(index_refresh._path(token).read_bytes())
        edits = [lambda value: value.update(extra=True), lambda value: value.pop('warning'),
                 lambda value: value.update(version=True),
                 lambda value: value['captured'][0].update(generation=True),
                 lambda value: value['captured'].append(value['captured'][0].copy()),
                 lambda value: value.update(expires_at=value['created_at'] + 1),
                 lambda value: value.update(lexical_ok=True),
                 lambda value: value.update(config_identity=''),
                 lambda value: value['captured'][0].update(root='/tmp/../escape')]
        for edit in edits:
            value = json.loads(json.dumps(original))
            edit(value)
            index_refresh._path(token).write_text(json.dumps(value))
            with self.subTest(value=value), mock.patch.object(freshness, 'acknowledge') as ack:
                with self.assertRaises(ValueError):
                    index_refresh.finish(token, lexical_ok=True, vector_ok=True)
                ack.assert_not_called()
        self.assertEqual(self.generations(), (1, 0, 0))

    def test_invalid_tokens_and_names_cannot_escape_private_state(self):
        for token in ('../escape', str(uuid.uuid1()), str(uuid.uuid4()).upper(), 'bad'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                index_refresh.finish(token, lexical_ok=True)
        for index in ('../escape', '/absolute', 'two words', 'x' * 129, None):
            with self.subTest(index=index), self.assertRaises(ValueError):
                index_refresh.begin(index)
        self.assertFalse(self.state.exists())

    def test_lease_io_rejects_links_public_modes_oversize_and_duplicate_fields(self):
        token = self.begin()
        path = index_refresh._path(token)
        original = path.read_bytes()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        variants = ('symlink', 'hardlink', 'mode', 'oversize', 'duplicate', 'depth', 'fifo')
        for variant in variants:
            with self.subTest(variant=variant):
                path.unlink(missing_ok=True)
                path.write_bytes(original)
                path.chmod(0o600)
                other = self.state / 'other'
                other.unlink(missing_ok=True)
                if variant == 'symlink':
                    path.replace(other)
                    path.symlink_to(other)
                elif variant == 'hardlink':
                    os.link(path, other)
                elif variant == 'mode':
                    path.chmod(0o644)
                elif variant == 'oversize':
                    path.write_bytes(b' ' * (index_refresh.MAX_LEASE_BYTES + 1))
                elif variant == 'duplicate':
                    path.write_bytes(b'{"version":1,' + original[1:])
                elif variant == 'depth':
                    path.write_bytes(b'[' * 2000 + b']' * 2000)
                else:
                    path.unlink()
                    os.mkfifo(path, mode=0o600)
                with mock.patch.object(freshness, 'acknowledge') as ack:
                    with self.assertRaises((OSError, ValueError)):
                        index_refresh.finish(token, lexical_ok=True)
                    ack.assert_not_called()

    def test_lease_lock_has_deadline_and_can_be_retried_after_release(self):
        token = self.begin()
        descriptor = os.open(index_refresh._path(token).with_suffix('.lock'), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(index_refresh, 'LOCK_TIMEOUT', .02):
                with self.assertRaises(TimeoutError):
                    index_refresh.finish(token, lexical_ok=True, vector_ok=True)
            self.assertEqual(self.generations(), (1, 0, 0))
        finally:
            os.close(descriptor)
        index_refresh.finish(token, lexical_ok=True, vector_ok=True)
        self.assertEqual(self.generations(), (1, 1, 1))

    def test_concurrent_finish_serializes_and_second_call_replays(self):
        token = self.begin()
        entered, release = threading.Event(), threading.Event()
        original = freshness.acknowledge
        results, failures = [], []
        def paused(generation, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Test release deadline')
            return original(generation, **kwargs)
        def run():
            try:
                results.append(index_refresh.finish(token, lexical_ok=True, vector_ok=True))
            except Exception as exc:
                failures.append(exc)
        with mock.patch.object(freshness, 'acknowledge', side_effect=paused) as ack:
            first = threading.Thread(target=run)
            second = threading.Thread(target=run)
            first.start()
            self.assertTrue(entered.wait(3))
            second.start()
            try:
                self.assertEqual(ack.call_count, 1)
            finally:
                release.set()
                first.join(3)
                second.join(3)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(ack.call_count, 2)
        self.assertFalse(failures)
        self.assertEqual(sorted(result['replayed'] for result in results), [False, True])
        self.assertEqual(self.generations(), (1, 1, 1))

    def test_cli_token_only_stdout_warning_stderr_and_no_stage_finish(self):
        self.configure(pattern='*.md')
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(entry._knowledge_main(['index-refresh', 'begin', '--index', '']), 0)
        token = stdout.getvalue().strip()
        self.assertEqual(str(uuid.UUID(token)), token)
        self.assertEqual(stdout.getvalue(), token + '\n')
        self.assertIn('no freshness will be acknowledged', stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(entry._knowledge_main(['index-refresh', 'finish', '--token', token]), 0)
        self.assertEqual(json.loads(stdout.getvalue())['acknowledged'], {'lexical': 0, 'vector': 0})

    def test_entry_is_config_light_without_an_implicit_vault(self):
        script = '''
import json, sys
from pathlib import Path
from claude_note import entry, index_refresh
index_refresh.STATE_ROOT = Path(sys.argv[1])
result = entry._knowledge_main(['index-refresh', 'begin'])
assert result == 0
assert 'claude_note.config' not in sys.modules
'''
        environment = {**os.environ, 'CLAUDE_NOTE_VAULT': '', 'CLAUDE_NOTE_VAULT_ROOT': '',
                       'PYTHONPATH': str(Path(index_refresh.__file__).resolve().parents[1])}
        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.state)],
            capture_output=True, text=True, env=environment, cwd=self.root, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(uuid.UUID(child.stdout.strip()).version, 4)


if __name__ == '__main__':
    unittest.main()
