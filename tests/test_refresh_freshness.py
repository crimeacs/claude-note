"""Synthetic production-refresh integration; no QMD/provider/private files."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_note import freshness, index_refresh, mutation, recall, setup_recall


class RefreshFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-note-refresh-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.qmd = self.root / "qmd"
        self.qmd.mkdir()
        self.share = self.root / "share"
        self.share.mkdir()
        environment = mock.patch.dict(os.environ, {"QMD_CONFIG_DIR": str(self.qmd),
            "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_CACHE_HOME": str(self.root / "cache"),
            "INDEX_PATH": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.configure()
        self.add_note("first")

    def configure(self, *, pattern="**/*.md", index="", yaml=False, root=None, ignore=None):
        path = self.qmd / f"{index or 'index'}.yml"
        if yaml:
            text = f'collections:\n  notes:\n    path: "{root or self.vault}"\n    pattern: "{pattern}"\n'
            if ignore is not None:
                text += "    ignore: " + json.dumps(ignore) + "\n"
            path.write_text(text)
        else:
            path.write_text(json.dumps({"collections": {"notes": {
                "path": str(root or self.vault), "pattern": pattern, "ignore": ignore}}}))

    def add_note(self, name, *, vault=None):
        mutation.create_note(name + ".md", "Synthetic " + name, vault_root=vault or self.vault,
                             operation_id=hashlib.sha256(name.encode()).hexdigest(),
                             allow_protected=any(part.startswith(".") for part in Path(name).parts))

    def refresh(self, results, *, embed=True, index=""):
        with mock.patch.multiple(setup_recall, SHARE=self.share,
                LOCKDIR=self.share / "refresh.lock", HEARTBEAT=self.share / "refresh.ok",
                REFRESH_LOG=self.share / "refresh.log"), \
             mock.patch.object(setup_recall, "qmd_exe", return_value="/synthetic/qmd"), \
             mock.patch.object(setup_recall, "_daily_eval"), \
             mock.patch.object(index_refresh, "STATE_ROOT", self.root / "leases"), \
             mock.patch("claude_note.index_proof.verify", return_value={
                 "lexical": True, "vector": True, "reason": ""}), \
             mock.patch.object(recall.Settings, "load", return_value=recall.Settings(index=index)), \
             mock.patch.object(setup_recall.subprocess, "run", side_effect=results) as command:
            result = setup_recall.refresh(embed=embed)
        return result, command

    def test_success_acknowledges_exact_registered_root_for_json_and_yaml(self):
        for yaml in (False, True):
            with self.subTest(yaml=yaml):
                self.configure(yaml=yaml)
                result, command = self.refresh([SimpleNamespace(returncode=0)] * 2)
                state = freshness.snapshot(self.vault)
                self.assertEqual(result, 0)
                self.assertEqual(command.call_count, 2)
                self.assertEqual(state["lexical_generation"], state["saved_generation"])
                self.assertEqual(state["vector_generation"], state["saved_generation"])
                self.assertFalse(state["dirty"])

    def test_writes_during_update_and_embed_remain_dirty(self):
        def run(command, **kwargs):
            self.add_note("during-" + command[-1])
            return SimpleNamespace(returncode=0)
        result, _ = self.refresh(run)
        state = freshness.snapshot(self.vault)
        self.assertEqual(result, 0)
        self.assertEqual(state["saved_generation"], 3)
        self.assertEqual(state["lexical_generation"], 1)
        self.assertEqual(state["vector_generation"], 1)
        self.assertEqual(set(state["dirty"]), {"during-update.md", "during-embed.md"})

    def test_embed_failure_preserves_successful_lexical_acknowledgement(self):
        result, _ = self.refresh([SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)])
        state = freshness.snapshot(self.vault)
        self.assertEqual(result, 1)
        self.assertEqual(state["lexical_generation"], 1)
        self.assertEqual(state["vector_generation"], 0)
        self.assertEqual(set(state["dirty"]), {"first.md"})

    def test_update_failure_does_not_acknowledge_or_attempt_embed(self):
        result, command = self.refresh([SimpleNamespace(returncode=1)])
        state = freshness.snapshot(self.vault)
        self.assertEqual(result, 1)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(state["lexical_generation"], 0)
        self.assertEqual(state["vector_generation"], 0)

    def test_no_embed_acknowledges_only_lexical_generation(self):
        result, command = self.refresh([SimpleNamespace(returncode=0)], embed=False)
        state = freshness.snapshot(self.vault)
        self.assertEqual(result, 0)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(state["lexical_generation"], 1)
        self.assertEqual(state["vector_generation"], 0)

    def test_filtered_and_unregistered_roots_are_not_acknowledged(self):
        other = self.root / "other"
        other.mkdir()
        self.add_note("other", vault=other)
        for pattern in ("*.md", "projects/**/*.md"):
            with self.subTest(pattern=pattern):
                self.configure(pattern=pattern)
                self.refresh([SimpleNamespace(returncode=0)] * 2)
                self.assertEqual(freshness.snapshot(self.vault)["lexical_generation"], 0)
                self.assertEqual(freshness.snapshot(other)["lexical_generation"], 0)

    def test_named_index_does_not_acknowledge_default_index_collection(self):
        other = self.root / "other"
        other.mkdir()
        self.add_note("other", vault=other)
        self.configure(index="isolated", root=other)
        result, command = self.refresh([SimpleNamespace(returncode=0)] * 2, index="isolated")
        self.assertEqual(result, 0)
        self.assertEqual(command.call_args_list[0].args[0], ["/synthetic/qmd", "--index", "isolated", "update"])
        self.assertEqual(freshness.snapshot(self.vault)["lexical_generation"], 0)
        self.assertEqual(freshness.snapshot(other)["lexical_generation"], 1)

    def test_collection_repointed_during_update_does_not_acknowledge_original_root(self):
        other = self.root / "other"
        other.mkdir()
        self.add_note("other", vault=other)
        def run(command, **kwargs):
            if command[-1] == "update":
                self.configure(root=other)
            return SimpleNamespace(returncode=0)
        result, _ = self.refresh(run)
        self.assertEqual(result, 0)
        for vault in (self.vault, other):
            state = freshness.snapshot(vault)
            self.assertEqual(state["lexical_generation"], 0)
            self.assertEqual(state["vector_generation"], 0)

    def test_config_change_during_capture_cannot_mix_roots_and_masks(self):
        other = self.root / "other"
        other.mkdir()
        self.add_note("other", vault=other)
        original = setup_recall.registered_collections
        def change(index):
            self.configure(root=other)
            return original(index)
        with mock.patch.object(setup_recall, "registered_collections", side_effect=change):
            captured, identity = setup_recall._freshness_capture("")
        self.assertEqual(captured, {})
        self.assertEqual(identity, "")
        for vault in (self.vault, other):
            state = freshness.snapshot(vault)
            self.assertEqual(state["lexical_generation"], 0)
            self.assertEqual(state["vector_generation"], 0)

    def test_custom_ignore_never_acknowledges_whole_root(self):
        for yaml in (False, True):
            with self.subTest(yaml=yaml):
                self.configure(yaml=yaml, ignore=["first.md"])
                self.assertEqual(setup_recall._freshness_capture("")[0], {})
                self.refresh([SimpleNamespace(returncode=0)] * 2)
                state = freshness.snapshot(self.vault)
                self.assertEqual(state["lexical_generation"], 0)
                self.assertEqual(state["vector_generation"], 0)
                self.assertIn("first.md", state["dirty"])

    def test_yaml_block_ignore_is_not_mistaken_for_empty_exclusions(self):
        for control in ('ignore:\n      - first.md', '"ignore": ["first.md"]',
                        "'ignore': ['first.md']", 'ignore : ["first.md"]', '<<: *excluded'):
            with self.subTest(control=control):
                self.configure(yaml=True)
                with (self.qmd / "index.yml").open("a") as handle:
                    handle.write("    " + control + "\n")
                self.assertEqual(setup_recall._freshness_collections("")[0], {})

    def test_fixed_directory_and_hidden_paths_keep_captured_root_dirty(self):
        excluded = ("build/lesson", "dist/lesson", "vendor/lesson", "node_modules/lesson", ".git/lesson", ".cache/lesson",
                    "nested/build/lesson", ".hidden/lesson", "nested/.hidden/lesson", ".hidden")
        for number, name in enumerate(excluded):
            with self.subTest(path=name):
                vault = self.root / ("excluded-" + str(number))
                vault.mkdir()
                self.add_note(name, vault=vault)
                self.configure(root=vault)
                self.assertEqual(setup_recall._freshness_capture("")[0], {})
                self.refresh([SimpleNamespace(returncode=0)] * 2)
                state = freshness.snapshot(vault)
                self.assertEqual(state["lexical_generation"], 0)
                self.assertEqual(state["vector_generation"], 0)
                self.assertIn(name + ".md", state["dirty"])

    def test_full_root_alias_is_eligible_even_when_another_alias_has_ignore(self):
        (self.qmd / "index.yml").write_text(json.dumps({"collections": {
            "filtered": {"path": str(self.vault), "pattern": "**/*.md", "ignore": ["first.md"]},
            "full": {"path": str(self.vault), "pattern": "**/*.md", "ignore": []}}}))
        eligible, _ = setup_recall._freshness_collections("")
        self.assertEqual(eligible, {"full": self.vault})
        captured, _ = setup_recall._freshness_capture("")
        self.assertEqual(captured, {self.vault: 1})
        self.refresh([SimpleNamespace(returncode=0)] * 2)
        self.assertFalse(freshness.snapshot(self.vault)["dirty"])

    def test_newer_excluded_pending_mutation_does_not_invalidate_prior_capture(self):
        freshness.mark_pending("build/later.md", "later", vault_root=self.vault)
        captured, _ = setup_recall._freshness_capture("")
        self.assertEqual(captured, {self.vault: 1})
        self.refresh([SimpleNamespace(returncode=0)] * 2)
        state = freshness.snapshot(self.vault)
        self.assertEqual(state["lexical_generation"], 1)
        self.assertEqual(state["vector_generation"], 1)
        self.assertEqual(set(state["dirty"]), {"build/later.md"})

    def test_helper_failure_is_soft_and_lock_is_released(self):
        with mock.patch.object(index_refresh, "finish", side_effect=RuntimeError("Synthetic proof failure")):
            result, _ = self.refresh([SimpleNamespace(returncode=0)] * 2)
        self.assertEqual(result, 0)
        self.assertTrue((self.share / "refresh.ok").exists())
        self.assertFalse((self.share / "refresh.lock").exists())
        self.assertIn("not acknowledged: RuntimeError", (self.share / "refresh.log").read_text())
        self.assertEqual(freshness.snapshot(self.vault)["lexical_generation"], 0)

    def test_capture_failure_does_not_prevent_qmd_or_success_heartbeat(self):
        with mock.patch.object(index_refresh, "begin", side_effect=RuntimeError("Synthetic metadata failure")):
            result, command = self.refresh([SimpleNamespace(returncode=0)] * 2)
        self.assertEqual(result, 0)
        self.assertEqual(command.call_count, 2)
        self.assertTrue((self.share / "refresh.ok").exists())
        self.assertFalse((self.share / "refresh.lock").exists())
        self.assertEqual(freshness.snapshot(self.vault)["lexical_generation"], 0)

    def test_timeout_finishes_with_only_completed_stage_flags(self):
        import subprocess
        with mock.patch.object(index_refresh, "finish", wraps=index_refresh.finish) as finish:
            result, _ = self.refresh([SimpleNamespace(returncode=0), subprocess.TimeoutExpired("qmd", 10)])
        self.assertEqual(result, 1)
        self.assertEqual(finish.call_args.kwargs, {"lexical_ok": True, "vector_ok": False})
        self.assertEqual(freshness.snapshot(self.vault)["lexical_generation"], 1)
        self.assertEqual(freshness.snapshot(self.vault)["vector_generation"], 0)

    def test_stale_lock_is_reacquired_before_capture_and_indexing(self):
        lock = self.share / "refresh.lock"
        lock.mkdir()
        os.utime(lock, (0, 0))
        original = index_refresh.begin

        def begin(index):
            self.assertTrue(lock.is_dir())
            self.assertGreater(lock.stat().st_mtime, 0)
            return original(index)

        with mock.patch.object(index_refresh, "begin", side_effect=begin) as capture:
            result, command = self.refresh([SimpleNamespace(returncode=0)] * 2)
        self.assertEqual(result, 0)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(command.call_count, 2)
        self.assertFalse(lock.exists())

    def test_stale_reacquisition_race_skips_without_removing_new_owners_lock(self):
        lock = self.share / "refresh.lock"
        lock.mkdir()
        os.utime(lock, (0, 0))
        original = Path.mkdir
        attempts = 0

        def race(path, *args, **kwargs):
            nonlocal attempts
            if path == lock:
                attempts += 1
                if attempts == 2:
                    original(path, *args, **kwargs)
                    raise FileExistsError("Synthetic competing refresh won")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", race), mock.patch.object(index_refresh, "begin") as capture:
            result, command = self.refresh([])
        self.assertEqual(result, 0)
        self.assertEqual(command.call_count, 0)
        capture.assert_not_called()
        self.assertTrue(lock.is_dir())

    def test_finish_and_warning_log_failure_still_release_owned_lock(self):
        original = setup_recall._log

        def fail_warning(line):
            if "not acknowledged" in line:
                raise OSError("Synthetic unavailable warning log")
            return original(line)

        with mock.patch.object(index_refresh, "finish", side_effect=RuntimeError("Synthetic helper failure")), \
             mock.patch.object(setup_recall, "_log", side_effect=fail_warning):
            with self.assertRaises(OSError):
                self.refresh([SimpleNamespace(returncode=0)] * 2)
        self.assertFalse((self.share / "refresh.lock").exists())

    def test_new_scope_reproves_retained_revisions_after_dirty_was_pruned(self):
        from claude_note import health
        self.refresh([SimpleNamespace(returncode=0)] * 2)
        before = freshness.snapshot(self.vault)
        self.assertFalse(before['dirty'])
        self.assertIn('first.md', before['retained_revisions'])
        config = self.qmd / 'index.yml'
        value = json.loads(config.read_text())
        value['models'] = {'embed': 'hf:synthetic/new-model/weights.gguf'}
        config.write_text(json.dumps(value))
        with mock.patch.object(recall.Settings, 'load', return_value=recall.Settings()):
            self.assertFalse(health.freshness_summary(self.vault)['vector_fresh'])
        self.refresh([SimpleNamespace(returncode=0)] * 2)
        after = freshness.snapshot(self.vault)
        self.assertNotEqual(before['vector_proof_scope'], after['vector_proof_scope'])
        self.assertEqual(after['vector_generation'], 1)
        with mock.patch.object(recall.Settings, 'load', return_value=recall.Settings()):
            self.assertTrue(health.freshness_summary(self.vault)['vector_fresh'])


if __name__ == "__main__":
    unittest.main()
