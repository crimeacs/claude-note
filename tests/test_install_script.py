"""scripts/install-from-checkout.sh from a bundled copy, on a machine with nothing set up.

Runs the real script against a throwaway HOME with fake `uv` and `launchctl`
first on PATH, so nothing is installed or loaded on the machine running the
tests.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/install-from-checkout.sh"

FAKE_UV = """#!/bin/sh
# tool install <root> ... | tool dir --bin | tool list
case "$1 $2" in
  "tool install")
    printf '#!/bin/sh\\nPYTHONPATH="%s/src" exec "%s" -m claude_note "$@"\\n' "$3" "{python}" > "$HOME/.local/bin/claude-note"
    chmod +x "$HOME/.local/bin/claude-note"
    echo "$3" >> "$HOME/uv-installs.log" ;;
  "tool dir") echo "$HOME/.local/bin" ;;
  "tool list") echo "claude-note v0" ;;
esac
"""

# Records calls; `print` fails until something was bootstrapped.
FAKE_LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$HOME/launchctl.log"
case "$1" in
  print) grep -q bootstrap "$HOME/launchctl.log" ;;
  *) exit 0 ;;
esac
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class BundledInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        bin_dir = self.home / ".local/bin"
        bin_dir.mkdir(parents=True)
        for name, body in (("uv", FAKE_UV.replace("{python}", sys.executable)), ("launchctl", FAKE_LAUNCHCTL)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        # A bundled copy: no .git, the ref in SOURCE_REF.
        self.bundle = self.tmp / "bundle"
        shutil.copytree(REPO, self.bundle, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
        (self.bundle / "SOURCE_REF").write_text("abc123def456\n")

    def run_script(self, *args):
        env = {"HOME": str(self.home), "PATH": f"{self.home}/.local/bin:/usr/bin:/bin", "LANG": "C"}
        return subprocess.run(["bash", str(self.bundle / SCRIPT), "--allow-temp", *args],
                              env=env, capture_output=True, text=True, timeout=120)

    def test_fresh_machine_then_idempotent_rerun(self):
        vault = self.home / "notes"
        args = ("--non-interactive", "--vault", str(vault), "--author", "teammate@example.com",
                "--push-agent", "--claude-hooks")
        first = self.run_script(*args)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)

        config = (self.home / ".config/claude-note/config.toml").read_text()
        self.assertIn(f'vault_root = "{vault}"', config)
        self.assertTrue(config.startswith('author = "teammate@example.com"'))
        self.assertTrue((vault / ".claude-note/queue").is_dir())
        self.assertTrue((vault / "CLAUDE.md").exists())
        installed = json.loads((self.home / ".local/share/claude-note/installed-from.json").read_text())
        self.assertEqual((installed["source"], installed["commit"]), ("bundle", "abc123def456"))
        settings = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertIn("claude-note enqueue", json.dumps(settings["hooks"]["Stop"]))

        if sys.platform == "darwin":
            agents = self.home / "Library/LaunchAgents"
            self.assertTrue((agents / "com.claude-note.worker.plist").exists())
            self.assertTrue((agents / "com.claude-note.push.plist").exists())

        second = self.run_script(*args)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual((self.home / ".config/claude-note/config.toml").read_text(), config)
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text()), settings)
        self.assertEqual(list((self.home / ".claude").glob("settings.json.bak-*")), [])
        self.assertIn("already has the claude-note hooks", second.stdout)

        status = subprocess.run([str(self.home / ".local/bin/claude-note"), "status", "--json"],
                                env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
                                capture_output=True, text=True, timeout=60)
        report = json.loads(status.stdout)
        self.assertTrue(report["configured"])
        self.assertEqual(report["author"], "teammate@example.com")
        self.assertTrue(report["hooks"]["claude_code"])

    def test_author_changes_in_place(self):
        self.assertEqual(self.run_script("--non-interactive", "--author", "a@example.com").returncode, 0)
        out = self.run_script("--author", "b@example.com")
        self.assertEqual(out.returncode, 0, out.stderr)
        config = (self.home / ".config/claude-note/config.toml").read_text()
        self.assertIn('author = "b@example.com"', config)
        self.assertNotIn("a@example.com", config)

    def test_rejects_a_bad_author_before_installing(self):
        out = self.run_script("--non-interactive", "--author", 'x"; rm -rf ~ #')
        self.assertEqual(out.returncode, 2)
        self.assertFalse((self.home / "uv-installs.log").exists())

    def test_update_only_mode_writes_no_config(self):
        out = self.run_script()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse((self.home / ".config/claude-note/config.toml").exists())


if __name__ == "__main__":
    unittest.main()
