"""scripts/install-from-checkout.sh from a bundled copy, on a machine with nothing set up.

Runs a synthetic copy of the script with home references redirected to a
task-specific variable, fake `uv`/`launchctl`, and scoped Python home mocks.
No HOME environment override, real installation or loaded job is used.
"""

import json
import plistlib
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
    printf '%s' "$3" > "$HOME/installed-source.txt"
    printf '#!/bin/sh\\nexec "%s" "%s/python-launcher.py" "$@"\\n' "{python}" "$HOME" > "$HOME/.local/bin/claude-note"
    chmod +x "$HOME/.local/bin/claude-note"
    echo "$3" >> "$HOME/uv-installs.log" ;;
  "tool dir") echo "$HOME/.local/bin" ;;
  "tool list") echo "claude-note v0" ;;
esac
"""

# Records and simulates individual jobs; no actual service manager is called.
FAKE_LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$HOME/launchctl.log"
case "$1" in
  print)
    if test -f "$HOME/fail-launchctl-query"; then echo "Operation not permitted" >&2; exit 1; fi
    if ! test -f "$HOME/loaded-${2##*/}"; then
      echo "Could not find service \\\"${2##*/}\\\" in domain for user gui" >&2; exit 113
    fi ;;
  bootstrap|load)
    target="${3:-$2}"
    label="${target##*/}"; label="${label%.plist}"
    if test "$label" = com.claude-note.recall-server && test -f "$HOME/fail-recall-bootstrap"; then
      echo "synthetic bootstrap failure" >&2; exit 5
    fi
    if test "$label" = com.claude-note.worker && test -f "$HOME/fail-worker-bootstrap"; then
      echo "synthetic worker bootstrap failure" >&2; exit 5
    fi
    touch "$HOME/loaded-$label" ;;
  bootout|unload)
    target="${3:-$2}"
    label="${target##*/}"; label="${label%.plist}"
    rm -f "$HOME/loaded-$label" ;;
  kickstart)
    if test -f "$HOME/fail-worker-kickstart"; then echo "synthetic worker restart failure" >&2; exit 5; fi ;;
  *) exit 0 ;;
esac
"""

PYTHON_LAUNCHER = """import os,sys,runpy
from pathlib import Path
from unittest import mock
root=Path(os.environ['CLAUDE_NOTE_TEST_HOME'])
checkout=(root/'installed-source.txt').read_text()
sys.path.insert(0,str(Path(checkout)/'src'))
original=os.path.expanduser
def expanduser(value):
    text=os.fspath(value)
    return str(root)+text[1:] if text=='~' or text.startswith('~/') else original(value)
with mock.patch.object(Path,'home',return_value=root), mock.patch.object(os.path,'expanduser',side_effect=expanduser):
    runpy.run_module('claude_note',run_name='__main__')
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
            (bin_dir / name).write_text(body.replace('$HOME', '$CLAUDE_NOTE_TEST_HOME'))
            (bin_dir / name).chmod(0o755)
        (self.home / 'python-launcher.py').write_text(PYTHON_LAUNCHER)
        # A bundled copy: no .git, the ref in SOURCE_REF.
        self.bundle = self.tmp / "bundle"
        shutil.copytree(REPO, self.bundle, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
        installer = self.bundle / SCRIPT
        installer.write_text(installer.read_text().replace('$HOME', '$CLAUDE_NOTE_TEST_HOME'))
        (self.bundle / "SOURCE_REF").write_text("abc123def456\n")

    def run_script(self, *args):
        env = {"CLAUDE_NOTE_TEST_HOME": str(self.home), "PATH": f"{self.home}/.local/bin:/usr/bin:/bin", "LANG": "C"}
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
                                env={"CLAUDE_NOTE_TEST_HOME": str(self.home), "PATH": "/usr/bin:/bin"},
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
        self.assertFalse((self.home / 'Library/LaunchAgents/com.claude-note.recall-server.plist').exists())
        if sys.platform == 'darwin':
            self.assertIn('Recall server not configured; left absent', out.stdout)
            self.assertNotIn('recall-server.plist', (self.home / 'launchctl.log').read_text())

    def recall_runtime(self, *, plist=True, loaded=True):
        binary = self.home / '.local/bin'
        for name, body in (('qmd', '#!/bin/sh\necho qmd 2.8.3\n'),
                           ('node', '#!/bin/sh\nprintf "%s\\n" "' + str(binary / 'node') + '"\n')):
            (binary / name).write_text(body)
            (binary / name).chmod(0o755)
        sdk = binary / 'node_modules/@tobilu/qmd/dist'
        sdk.mkdir(parents=True)
        for name in ('index.js', 'llm.js'):
            (sdk / name).write_text('// synthetic installed SDK; never executed\n')
        (sdk.parent / 'package.json').write_text('{"name":"@tobilu/qmd","version":"2.8.3"}')
        agent = self.home / 'Library/LaunchAgents/com.claude-note.recall-server.plist'
        if plist:
            agent.parent.mkdir(parents=True)
            agent.write_bytes(plistlib.dumps({'Label': 'com.claude-note.recall-server',
                'ProgramArguments': ['/old/node', '/old/recall_server.mjs', '/old/sdk.js', '8282']}))
        if loaded:
            (self.home / 'loaded-com.claude-note.recall-server').touch()
        state = self.home / '.local/share/claude-note'
        state.mkdir(parents=True)
        receipt = state / 'installed-from.json'
        receipt.write_text('{"commit":"old-completed"}\n')
        return agent, receipt

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd upgrade is macOS-only')
    def test_existing_recall_runtime_uses_new_package_and_keeps_owner_data(self):
        renamed = self.tmp / 'bundle & recalled'
        self.bundle.rename(renamed)
        self.bundle = renamed
        agent, receipt = self.recall_runtime()
        config = self.home / '.config/claude-note/config.toml'
        config.parent.mkdir(parents=True)
        vault = self.home / 'owner-notes'
        vault.mkdir()
        note = vault / 'human.md'; note.write_text('Human correction stays unchanged.\n')
        config.write_text('vault_root = ' + json.dumps(str(vault)) + '\n[egress]\ndefault = ["host"]\n')
        previous_config = config.read_bytes()
        out = self.run_script()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        refreshed = plistlib.loads(agent.read_bytes())
        self.assertEqual(refreshed['ProgramArguments'][1], str(self.bundle.resolve() / 'src/claude_note/recall_server.mjs'))
        self.assertEqual(refreshed['ProgramArguments'][-1], '8282')
        self.assertEqual(config.read_bytes(), previous_config)
        self.assertEqual(note.read_text(), 'Human correction stays unchanged.\n')
        self.assertEqual(json.loads(receipt.read_text())['commit'], 'abc123def456')
        self.assertIn('Recall server upgrade: installed', out.stdout)
        calls = (self.home / 'launchctl.log').read_text()
        self.assertIn('bootout', calls)
        self.assertIn('bootstrap', calls)
        self.assertNotIn('qmd-refresh', calls)
        self.assertNotIn('nightly', calls)

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd upgrade is macOS-only')
    def test_plist_only_upgrades_and_loaded_only_without_config_fails_closed(self):
        agent, receipt = self.recall_runtime(plist=True, loaded=False)
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        completed = receipt.read_bytes()
        agent.unlink()
        second = self.run_script()
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('restore its configuration', second.stdout + second.stderr)
        self.assertFalse(agent.exists())
        self.assertEqual(receipt.read_bytes(), completed)

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd upgrade is macOS-only')
    def test_failed_recall_restart_keeps_previous_completed_receipt(self):
        agent, receipt = self.recall_runtime()
        previous = receipt.read_bytes()
        (self.home / 'fail-recall-bootstrap').touch()
        out = self.run_script()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('synthetic bootstrap failure', out.stdout + out.stderr)
        self.assertIn('installation remains incomplete', out.stderr)
        self.assertEqual(receipt.read_bytes(), previous)
        self.assertEqual(list(receipt.parent.glob('installed-from.????????')), [])
        self.assertNotIn('Installed claude-note', out.stdout)
        (self.home / 'fail-recall-bootstrap').unlink()
        retry = self.run_script()
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(json.loads(receipt.read_text())['commit'], 'abc123def456')

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd upgrade is macOS-only')
    def test_custom_recall_endpoint_index_environment_and_service_settings_survive(self):
        agent, receipt = self.recall_runtime()
        previous = plistlib.loads(agent.read_bytes())
        previous['ProgramArguments'].extend(['/synthetic/private-index.sqlite', '/synthetic/private-index.yml'])
        previous['EnvironmentVariables'] = {'QMD_CONFIG_DIR': '/synthetic/config', 'CLAUDE_NOTE_INDEX_ID': 'isolated',
                                            'XDG_CACHE_HOME': '/synthetic/cache', 'QMD_METAL_KEEP_RESIDENCY': '0'}
        previous.update({'WorkingDirectory': '/synthetic/work', 'StandardOutPath': '/synthetic/out.log',
                         'StandardErrorPath': '/synthetic/error.log', 'Nice': 8})
        agent.write_bytes(plistlib.dumps(previous))
        out = self.run_script()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        current = plistlib.loads(agent.read_bytes())
        self.assertEqual(current['ProgramArguments'][3:], previous['ProgramArguments'][3:])
        for key in ('EnvironmentVariables', 'WorkingDirectory', 'StandardOutPath', 'StandardErrorPath', 'Nice'):
            self.assertEqual(current[key], previous[key])

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd upgrade is macOS-only')
    def test_query_or_worker_failure_never_publishes_completed_receipt(self):
        agent, receipt = self.recall_runtime()
        previous = receipt.read_bytes()
        for marker, args in (('fail-launchctl-query', ()), ('fail-worker-kickstart', ()),
                             ('fail-worker-bootstrap', ('--non-interactive', '--vault', str(self.home / 'notes')))):
            loaded_worker = self.home / 'loaded-com.claude-note.worker'
            if marker == 'fail-worker-kickstart': loaded_worker.touch()
            else: loaded_worker.unlink(missing_ok=True)
            (self.home / marker).touch()
            out = self.run_script(*args)
            with self.subTest(marker=marker):
                self.assertNotEqual(out.returncode, 0, out.stdout + out.stderr)
                self.assertEqual(receipt.read_bytes(), previous)
            (self.home / marker).unlink()

    @unittest.skipUnless(sys.platform == 'darwin', 'launchd upgrade is macOS-only')
    def test_unsupported_sdk_fails_before_replacing_live_agent(self):
        agent, receipt = self.recall_runtime()
        previous, completed = agent.read_bytes(), receipt.read_bytes()
        package = self.home / '.local/bin/node_modules/@tobilu/qmd/package.json'
        package.write_text('{"version":"2.9.0"}')
        out = self.run_script()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('SDK does not match', out.stdout + out.stderr)
        self.assertEqual(agent.read_bytes(), previous)
        self.assertEqual(receipt.read_bytes(), completed)
        self.assertNotIn('bootout', (self.home / 'launchctl.log').read_text())

    def test_systemd_worker_failure_keeps_prior_completed_receipt(self):
        agent, receipt = self.recall_runtime()
        previous = receipt.read_bytes()
        binary = self.home / '.local/bin'
        for name, body in (('uname', '#!/bin/sh\necho Linux\n'),
                           ('systemctl', '#!/bin/sh\nif test "$2" = restart; then echo "synthetic restart failure" >&2; exit 5; fi\nexit 0\n')):
            (binary / name).write_text(body)
            (binary / name).chmod(0o755)
        out = self.run_script()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('could not restart the worker', out.stderr)
        self.assertEqual(receipt.read_bytes(), previous)

    def test_missing_starters_are_added_without_replacing_vault_preferences(self):
        template = self.bundle / "vault-template"
        (template / ".obsidian").mkdir(exist_ok=True)
        (template / ".obsidian/graph.json").write_text('{"starter": true}')
        (template / ".obsidian/types.json").write_text('{"types": {}}')
        (template / "templates/new-type.md").write_text("new starter")
        vault = self.home / "notes"
        (vault / ".obsidian").mkdir(parents=True)
        (vault / ".obsidian/graph.json").write_text('{"custom": true}')
        (vault / "templates").mkdir()
        (vault / "templates/new-type.md").write_text("custom template")
        result = self.run_script("--non-interactive", "--vault", str(vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((vault / ".obsidian/graph.json").read_text(), '{"custom": true}')
        self.assertEqual((vault / "templates/new-type.md").read_text(), "custom template")
        self.assertTrue((vault / ".obsidian/types.json").exists())
        self.assertTrue((vault / "templates/decision.md").exists())

    def test_source_metadata_escapes_paths_and_worker_plist_escapes_vault(self):
        renamed = self.tmp / 'bundle "quoted"'
        self.bundle.rename(renamed)
        self.bundle = renamed
        vault = self.home / "Research & Notes"
        result = self.run_script("--non-interactive", "--vault", str(vault))
        self.assertEqual(result.returncode, 0, result.stderr)
        source = json.loads((self.home / ".local/share/claude-note/installed-from.json").read_text())
        self.assertEqual(source["path"], str(self.bundle.resolve()))
        self.assertEqual(source["repository"], "https://github.com/crimeacs/claude-note.git")
        if sys.platform == "darwin":
            with (self.home / "Library/LaunchAgents/com.claude-note.worker.plist").open("rb") as f:
                worker = plistlib.load(f)
            self.assertTrue(worker["StandardOutPath"].startswith(str(vault)))
            # issue #3: the worker, and every `claude -p` it starts, never runs from /
            self.assertEqual(worker["WorkingDirectory"], str(vault / ".claude-note/tmp"))
            self.assertTrue((vault / ".claude-note/tmp").is_dir())


if __name__ == "__main__":
    unittest.main()
