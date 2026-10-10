import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CLAUDE_NOTE_VAULT_ROOT", tempfile.mkdtemp())

from claude_note import cli, version_checker  # noqa: E402


class UpdateSourceTests(unittest.TestCase):
    def test_current_repository_is_the_maintained_fork(self):
        self.assertEqual(version_checker.REPO_URL, "https://github.com/crimeacs/claude-note")
        self.assertIn("repos/crimeacs/claude-note/", version_checker.RELEASES_API)

    def test_update_preserves_recorded_git_and_bundle_sources(self):
        for kind in ("git", "bundle"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as home:
                provenance = Path(home) / ".local/share/claude-note/installed-from.json"
                provenance.parent.mkdir(parents=True)
                provenance.write_text(json.dumps({"source": kind, "path": "/some/own fork",
                                                  "commit": "pinned-ref"}))
                output = io.StringIO()
                with mock.patch.object(version_checker.Path, "home", return_value=Path(home)), \
                        mock.patch.object(version_checker, "get_latest_version") as network, \
                        mock.patch.object(cli.subprocess, "run") as installer, redirect_stdout(output):
                    self.assertEqual(cli.cmd_update(SimpleNamespace(no_restart=True)), 1)
                network.assert_not_called()
                installer.assert_not_called()
                self.assertIn("pinned-ref", output.getvalue())
                if kind == "git":
                    self.assertIn("'/some/own fork/scripts/install-from-checkout.sh'", output.getvalue())
                else:
                    self.assertIn("SOURCE_REF", output.getvalue())

    def test_unmanaged_update_installs_advertised_fork_release(self):
        with mock.patch.object(version_checker, "managed_source", return_value=None), \
                mock.patch.object(version_checker, "get_latest_version", return_value="99.0.0"), \
                mock.patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_update(SimpleNamespace(no_restart=True)), 0)
        self.assertEqual(run.call_args.args[0][-1],
                         "git+https://github.com/crimeacs/claude-note.git@v99.0.0")

    def test_update_cache_from_another_repository_is_invalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "version-check.json"
            cache.write_text(json.dumps({"last_check": datetime.now(timezone.utc).isoformat(),
                                         "repository": "https://github.com/artemiin/claude-note"}))
            with mock.patch.object(version_checker, "VERSION_CHECK_FILE", cache):
                self.assertTrue(version_checker.should_check())
                version_checker.save_check_result("1.0.0", False)
                self.assertFalse(version_checker.should_check())


if __name__ == "__main__":
    unittest.main()
