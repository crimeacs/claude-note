"""Single-index journal ownership and offline health, using synthetic vaults."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from claude_note import freshness, health, index_refresh, mutation, recall


class FreshnessOwnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='claude-note-owner-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.environment = mock.patch.dict(os.environ, {
            'QMD_CONFIG_DIR': str(self.root / 'qmd'),
            'XDG_CACHE_HOME': str(self.root / 'cache'), 'INDEX_PATH': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = mock.patch.object(recall.Settings, 'load', return_value=recall.Settings())
        self.settings.start()
        self.addCleanup(self.settings.stop)
        configuration = self.root / 'qmd/index.yml'
        configuration.parent.mkdir()
        configuration.write_text(json.dumps({'collections': {'notes': {
            'path': str(self.vault), 'pattern': '**/*.md'}}}))
        self.owner = index_refresh.owner_identity()
        mutation.create_note('lesson.md', 'Synthetic lesson', vault_root=self.vault,
                             operation_id='lesson')
        self.manifest = self.vault / '.claude-note/mutations/freshness/manifest.json'

    def acknowledge(self, capability='lexical', owner=None):
        return freshness.acknowledge(1, capability=capability, vault_root=self.vault, index_owner=owner,
                                     proof_scope=index_refresh.proof_identity() if owner is not None else None)

    def test_owner_binding_does_not_inherit_unscoped_capabilities(self):
        self.acknowledge('lexical')
        self.acknowledge('vector')
        state = self.acknowledge(owner=self.owner)
        self.assertEqual(state['index_owner'], self.owner)
        self.assertEqual(state['lexical_generation'], 1)
        self.assertEqual(state['vector_generation'], 0)
        self.acknowledge('vector', self.owner)
        self.assertEqual(freshness.snapshot(self.vault)['vector_generation'], 1)

    def test_owner_cannot_be_changed_or_omitted_after_binding(self):
        self.acknowledge(owner=self.owner)
        body = self.manifest.read_bytes()
        for owner in (None, index_refresh.owner_identity('other')):
            with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, 'another index owner'):
                self.acknowledge('vector', owner)
            self.assertEqual(self.manifest.read_bytes(), body)

    def test_legacy_pruned_generation_sets_explicit_migration_baseline(self):
        self.acknowledge('lexical')
        self.acknowledge('vector')
        self.assertFalse(freshness.snapshot(self.vault)['dirty'])
        mutation.create_note('new.md', 'Synthetic new lesson', vault_root=self.vault,
                             operation_id='new')
        state = freshness.acknowledge(2, vault_root=self.vault, index_owner=self.owner,
                                     proof_scope=index_refresh.proof_identity())
        self.assertEqual(state['index_baseline_generation'], 1)
        self.assertEqual((state['lexical_generation'], state['vector_generation']), (2, 0))
        summary = health.freshness_summary(self.vault)
        self.assertEqual(summary['index_baseline_generation'], 1)
        self.assertIn('after index binding baseline generation 1 only', summary['evidence'])
        self.assertIn('do not prove earlier or preexisting', summary['evidence'])
        self.assertFalse(summary['vector_fresh'])

    def test_concurrent_first_bind_has_one_owner_without_mixed_capabilities(self):
        owners = [self.owner, index_refresh.owner_identity('other')]

        def attempt(owner):
            try:
                self.acknowledge('lexical', owner)
                self.acknowledge('vector', owner)
                return owner
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, owners))
        self.assertEqual(sum(result is not None for result in results), 1)
        state = freshness.snapshot(self.vault)
        self.assertIn(state['index_owner'], owners)
        self.assertEqual(state['index_owner'], next(result for result in results if result))
        self.assertEqual((state['lexical_generation'], state['vector_generation']), (1, 1))

    def test_invalid_owner_rejects_before_effect(self):
        body = self.manifest.read_bytes()
        for owner in ('', 'x' * 64, 'A' * 64, 1, [], {}):
            with self.subTest(owner=owner), self.assertRaisesRegex(ValueError, 'Invalid index owner'):
                self.acknowledge(owner=owner)
            self.assertEqual(self.manifest.read_bytes(), body)

    def test_expiry_while_waiting_for_manifest_lock_cannot_bind_or_acknowledge(self):
        body = self.manifest.read_bytes()
        original = mutation.note_lock
        now = [9.0]

        @contextmanager
        def delayed_lock(*args, **kwargs):
            with original(*args, **kwargs):
                now[0] = 10.0
                yield

        with mock.patch.object(mutation, 'note_lock', delayed_lock), \
             mock.patch.object(freshness.time, 'time', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(ValueError, 'deadline expired'):
                freshness.acknowledge(1, vault_root=self.vault, index_owner=self.owner, deadline=10.0,
                                     proof_scope=index_refresh.proof_identity())
        self.assertEqual(self.manifest.read_bytes(), body)
        self.assertIsNone(freshness.snapshot(self.vault)['index_owner'])

    def test_deadline_is_finite_and_boolean_is_not_a_timestamp(self):
        body = self.manifest.read_bytes()
        for deadline in (True, '10', float('nan'), float('inf'), float('-inf')):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(ValueError, 'Invalid acknowledgement deadline'):
                freshness.acknowledge(1, vault_root=self.vault, index_owner=self.owner, deadline=deadline)
            self.assertEqual(self.manifest.read_bytes(), body)

    def test_health_requires_matching_owner_for_nonempty_journal(self):
        self.acknowledge('lexical')
        self.acknowledge('vector')
        summary = health.freshness_summary(self.vault)
        self.assertEqual(summary['index_binding_status'], 'unowned')
        self.assertFalse(summary['lexical_fresh'])
        self.assertFalse(summary['vector_fresh'])
        self.acknowledge('lexical', self.owner)
        self.acknowledge('vector', self.owner)
        summary = health.freshness_summary(self.vault)
        self.assertEqual(summary['index_binding_status'], 'bound')
        self.assertTrue(summary['lexical_fresh'])
        self.assertTrue(summary['vector_fresh'])
        (self.root / 'qmd/other.yml').write_text((self.root / 'qmd/index.yml').read_text())
        with mock.patch.object(recall.Settings, 'load', return_value=recall.Settings(index='other')):
            summary = health.freshness_summary(self.vault)
        self.assertEqual(summary['index_binding_status'], 'mismatch')
        self.assertFalse(summary['lexical_fresh'])
        self.assertFalse(summary['vector_fresh'])

    def test_config_content_changes_do_not_implicitly_transfer_owner(self):
        config = self.root / 'qmd/index.yml'
        config.parent.mkdir(exist_ok=True)
        config.write_text('collections: {}')
        self.assertEqual(index_refresh.owner_identity(), self.owner)
        config.write_text('collections: {different: {path: /synthetic}}')
        self.assertEqual(index_refresh.owner_identity(), self.owner)
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'other-cache')}):
            self.assertNotEqual(index_refresh.owner_identity(), self.owner)
        with mock.patch.dict(os.environ, {'QMD_CONFIG_DIR': str(self.root / 'other-config')}):
            self.assertNotEqual(index_refresh.owner_identity(), self.owner)

    def test_legacy_manifest_remains_readable_but_health_is_unverified(self):
        value = freshness.snapshot(self.vault)
        value.pop('index_owner', None)
        value['lexical_generation'] = value['vector_generation'] = 1
        mutation._metadata(self.vault, 'freshness', 'manifest', value)
        self.assertEqual(freshness.snapshot(self.vault)['saved_generation'], 1)
        self.assertEqual(health.freshness_summary(self.vault)['index_binding_status'], 'unowned')
        self.assertFalse(health.freshness_summary(self.vault)['lexical_fresh'])

    def test_malformed_stored_owner_fails_closed(self):
        value = freshness.snapshot(self.vault)
        value['index_owner'] = {'index': 'untrusted'}
        mutation._metadata(self.vault, 'freshness', 'manifest', value)
        with self.assertRaisesRegex(ValueError, 'Invalid freshness index owner'):
            freshness.snapshot(self.vault)
        summary = health.freshness_summary(self.vault)
        self.assertEqual(summary['index_binding_status'], 'unavailable')
        self.assertFalse(summary['lexical_fresh'])

    def test_health_does_not_claim_freshness_under_ambiguous_qmd_selection(self):
        self.acknowledge('lexical', self.owner)
        self.acknowledge('vector', self.owner)
        for values in ({'INDEX_PATH': str(self.root / 'other.sqlite')},
                       {'QMD_CONFIG_DIR': 'relative-config'}, {'XDG_CACHE_HOME': 'relative-cache'}):
            with self.subTest(values=values), mock.patch.dict(os.environ, values):
                summary = health.freshness_summary(self.vault)
                self.assertEqual(summary['index_binding_status'], 'unavailable')
                self.assertFalse(summary['lexical_fresh'])
                self.assertFalse(summary['vector_fresh'])
                self.assertTrue(summary['index_binding_warning'])
        local = self.root / '.qmd'
        local.mkdir()
        (local / 'index.yml').write_text('collections: {}')
        summary = health.freshness_summary(self.vault)
        self.assertEqual(summary['index_binding_status'], 'unavailable')
        self.assertFalse(summary['lexical_fresh'])
        self.assertFalse(summary['vector_fresh'])

    def test_changed_model_or_collection_scope_requires_new_proof(self):
        self.acknowledge('lexical', self.owner)
        self.acknowledge('vector', self.owner)
        self.assertFalse(freshness.snapshot(self.vault)['dirty'])
        self.assertIn('lesson.md', freshness.snapshot(self.vault)['retained_revisions'])
        config = self.root / 'qmd/index.yml'
        original = config.read_text()
        changed = json.loads(original)
        changed['models'] = {'embed': 'hf:synthetic/different-model/weights.gguf'}
        config.write_text(json.dumps(changed))
        summary = health.freshness_summary(self.vault)
        self.assertEqual(summary['index_binding_status'], 'bound')
        self.assertFalse(summary['lexical_fresh'])
        self.assertFalse(summary['vector_fresh'])
        self.acknowledge('lexical', self.owner)
        self.assertTrue(health.freshness_summary(self.vault)['lexical_fresh'])
        self.assertFalse(health.freshness_summary(self.vault)['vector_fresh'])
        self.acknowledge('vector', self.owner)
        self.assertTrue(health.freshness_summary(self.vault)['vector_fresh'])
        changed['collections']['notes']['ignore'] = ['lesson.md']
        config.write_text(json.dumps(changed))
        self.assertFalse(health.freshness_summary(self.vault)['lexical_fresh'])
        config.write_text(original)
        with mock.patch.dict(os.environ, {'QMD_EMBED_MODEL': 'hf:synthetic/override/weights.gguf'}):
            self.assertFalse(health.freshness_summary(self.vault)['vector_fresh'])

    def test_new_scope_cannot_inherit_later_generation_from_old_scope(self):
        mutation.create_note('new.md', 'Synthetic later lesson', vault_root=self.vault, operation_id='later')
        old_scope = index_refresh.proof_identity()
        freshness.acknowledge(2, vault_root=self.vault, index_owner=self.owner, proof_scope=old_scope)
        state = freshness.acknowledge(1, vault_root=self.vault, index_owner=self.owner, proof_scope='f' * 64)
        self.assertEqual(state['lexical_generation'], 1)
        self.assertEqual(state['lexical_proof_scope'], 'f' * 64)
        self.assertIn('new.md', state['dirty'])

    def test_owned_acknowledgement_requires_valid_proof_scope(self):
        body = self.manifest.read_bytes()
        for scope in (None, '', 'x' * 64, True, {}):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, 'proof scope'):
                freshness.acknowledge(1, vault_root=self.vault, index_owner=self.owner, proof_scope=scope)
            self.assertEqual(self.manifest.read_bytes(), body)
        with self.assertRaisesRegex(ValueError, 'requires an index owner'):
            freshness.acknowledge(1, vault_root=self.vault, proof_scope='f' * 64)


if __name__ == '__main__':
    unittest.main()
