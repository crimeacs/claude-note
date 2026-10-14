"""Synthetic production-refresh integration; no QMD/provider/private files."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from claude_note import freshness, mutation, recall, setup_recall


class RefreshFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-note-refresh-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.qmd = self.root / "qmd"
        self.qmd.mkdir()
        self.share = self.root / "share"
        self.share.mkdir()
        environment = mock.patch.dict(os.environ, {"QMD_CONFIG_DIR": str(self.qmd),
            "XDG_CONFIG_HOME": str(self.root / "config")})
        environment.start()
        self.addCleanup(environment.stop)
        self.configure()
        self.add_note("first")

    def configure(self, *, pattern="**/*.md", index="", yaml=False, root=None):
        path = self.qmd / f"{index or 'index'}.yml"
        if yaml:
            path.write_text(f'collections:\n  notes:\n    path: "{root or self.vault}"\n    pattern: "{pattern}"\n')
        else:
            path.write_text(json.dumps({"collections": {"notes": {
                "path": str(root or self.vault), "pattern": pattern}}}))

    def add_note(self, name, *, vault=None):
        mutation.create_note(name + ".md", "Synthetic " + name, vault_root=vault or self.vault,
                             operation_id=name)

    def refresh(self, results, *, embed=True, index=""):
        with mock.patch.multiple(setup_recall, SHARE=self.share,
                LOCKDIR=self.share / "refresh.lock", HEARTBEAT=self.share / "refresh.ok",
                REFRESH_LOG=self.share / "refresh.log"), \
             mock.patch.object(setup_recall, "qmd_exe", return_value="/synthetic/qmd"), \
             mock.patch.object(setup_recall, "_daily_eval"), \
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
            self.refresh([SimpleNamespace(returncode=0)] * 2)
        for vault in (self.vault, other):
            state = freshness.snapshot(vault)
            self.assertEqual(state["lexical_generation"], 0)
            self.assertEqual(state["vector_generation"], 0)


if __name__ == "__main__":
    unittest.main()
