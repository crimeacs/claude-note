"""Synthetic contracts for the shared installer interpreter selector.

Only a disposable Bash process and fake uv executable run. Python candidates
are executable sentinels: choosing a request must never execute them. The
harness uses a task-specific home variable and never overrides HOME.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts/install-python.sh"

FAKE_UV = r"""#!/bin/bash
set -eu
case "${1:-} ${2:-}" in
    'tool dir')
        printf 'dir\n' >> "$CLAUDE_NOTE_TEST_HOME/uv-calls"
        if [[ "${FAKE_DIR_RC:-0}" != 0 ]]; then exit "$FAKE_DIR_RC"; fi
        printf '%s\n' "${UV_TOOL_DIR:-$FAKE_TOOL_ROOT}"
        ;;
    'tool install')
        printf 'install\n' >> "$CLAUDE_NOTE_TEST_HOME/uv-calls"
        printf '%s\0' "$@" > "$CLAUDE_NOTE_TEST_HOME/install-argv"
        printf '%s\0' "${UV_PYTHON-__unset__}" "${UV_MANAGED_PYTHON-__unset__}" \
            "${UV_NO_MANAGED_PYTHON-__unset__}" "${UV_PYTHON_PREFERENCE-__unset__}" \
            > "$CLAUDE_NOTE_TEST_HOME/install-selectors"
        exit "${FAKE_INSTALL_RC:-0}"
        ;;
    *) echo 'unexpected synthetic uv command' >&2; exit 91 ;;
esac
"""

RUN_SELECTION = """set -eu
source "$1"
claude_note_select_install_python "$2"
uv tool install "$FAKE_SOURCE" --python "$CLAUDE_NOTE_INSTALL_PYTHON" --force --reinstall --quiet
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class InstallPythonSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "synthetic-home"
        self.home.mkdir()
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.tools = self.root / "tools"
        self.source = self.root / "source checkout"
        self.source.mkdir()
        uv = self.bin / "uv"
        uv.write_text(FAKE_UV)
        uv.chmod(0o755)
        for name in ("python", "python3", "python3.10", "python3.11"):
            candidate = self.bin / name
            candidate.write_text(
                '#!/bin/sh\nprintf "foreign interpreter ran\\n" > "$CLAUDE_NOTE_TEST_HOME/foreign-ran"\nexit 92\n'
            )
            candidate.chmod(0o755)

    def environment(self, **overrides):
        env = {
            "CLAUDE_NOTE_TEST_HOME": str(self.home),
            "FAKE_TOOL_ROOT": str(self.tools),
            "FAKE_SOURCE": str(self.source),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "LANG": "C",
        }
        env.update(overrides)
        self.assertNotIn("HOME", env)
        return env

    def run_selection(self, request="", **overrides):
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", RUN_SELECTION,
             "synthetic-selection", str(HELPER), request],
            cwd=self.root,
            env=self.environment(**overrides),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertFalse((self.home / "foreign-ran").exists())
        self.assertFalse((self.home / "base-ran").exists())
        return result

    def external_base(self, name="external-base/bin/python"):
        base = self.root / name
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(
            '#!/bin/sh\nprintf "base interpreter ran\\n" > "$CLAUDE_NOTE_TEST_HOME/base-ran"\nexit 93\n'
        )
        base.chmod(0o755)
        return base

    def runtime(self, *, tools=None):
        runtime = (tools or self.tools) / "claude-note/bin/python"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        return runtime

    def install_arguments(self):
        data = (self.home / "install-argv").read_bytes()
        self.assertTrue(data.endswith(b"\0"))
        return [part.decode() for part in data[:-1].split(b"\0")]

    def assert_selected(self, result, request):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.install_arguments(), [
            "tool", "install", str(self.source), "--python", request,
            "--force", "--reinstall", "--quiet",
        ])

    def assert_selection_failed(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.home / "install-argv").exists())
        self.assertNotIn("install", (self.home / "uv-calls").read_text().splitlines())

    def test_fresh_install_keeps_standard_python_request(self):
        self.assert_selected(self.run_selection(), "3.11")
        self.assertEqual((self.home / "uv-calls").read_text(), "dir\ninstall\n")

    def test_existing_absolute_external_base_is_reused_without_execution(self):
        base = self.external_base()
        self.runtime().symlink_to(base)
        self.assert_selected(self.run_selection(), str(base))

    def test_relative_multihop_links_resolve_to_external_base(self):
        base = self.external_base("base with spaces/bin/python3.11")
        middle = self.root / "links/second"
        middle.parent.mkdir()
        middle.symlink_to(os.path.relpath(base, middle.parent))
        first = self.root / "links/first"
        first.symlink_to("second")
        runtime = self.runtime()
        runtime.symlink_to(os.path.relpath(first, runtime.parent))
        self.assert_selected(self.run_selection(), str(base))

    def test_custom_tool_root_and_metacharacters_are_literal(self):
        self.tools = self.root / "tools ' \" $(touch injected-dollar) `touch injected-tick`;"
        self.source = self.root / "source ' \" $(touch injected-source);"
        self.source.mkdir()
        base = self.external_base("base ' \" $(touch injected-base);/bin/python")
        self.runtime().symlink_to(base)
        self.assert_selected(self.run_selection(UV_TOOL_DIR=str(self.tools)), str(base))
        for name in ("injected-dollar", "injected-tick", "injected-source", "injected-base"):
            self.assertFalse((self.root / name).exists())

    def test_tool_root_directory_alias_is_canonicalized_before_exclusion(self):
        self.tools.mkdir()
        alias = self.root / "tool-root-alias"
        alias.symlink_to(self.tools, target_is_directory=True)
        base = self.external_base()
        self.runtime().symlink_to(base)
        self.assert_selected(self.run_selection(UV_TOOL_DIR=str(alias)), str(base))

    def test_explicit_request_precedes_uv_python_and_skips_existing_tool(self):
        self.runtime().write_text("copied executable must not be selected\n")
        request = "/explicit chosen base/python"
        self.assert_selected(self.run_selection(request, UV_PYTHON="3.12"), request)
        self.assertEqual((self.home / "uv-calls").read_text(), "install\n")

    def test_uv_python_request_is_authoritative_without_installer_request(self):
        self.runtime().symlink_to("missing-base")
        self.assert_selected(self.run_selection(UV_PYTHON="3.12"), "3.12")
        self.assertEqual((self.home / "uv-calls").read_text(), "install\n")

    def test_selector_environment_opts_out_even_when_empty_or_false(self):
        self.runtime().symlink_to("missing-base")
        keys = ("UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "UV_PYTHON_PREFERENCE")
        for key in keys:
            for value in ("", "false", "true"):
                with self.subTest(key=key, value=value):
                    result = self.run_selection(**{key: value})
                    self.assert_selected(result, "3.11")
                    received = (self.home / "install-selectors").read_bytes()[:-1].split(b"\0")
                    expected = [b"__unset__"] * 4
                    expected[keys.index(key) + 1] = value.encode()
                    self.assertEqual(received, expected)
                    self.assertEqual((self.home / "uv-calls").read_text().splitlines()[-1], "install")
        self.assertNotIn("dir", (self.home / "uv-calls").read_text().splitlines())

    def test_selector_families_are_not_invented_or_combined(self):
        base = self.external_base()
        self.runtime().symlink_to(base)
        for selectors in ({"UV_MANAGED_PYTHON": "true"},
                          {"UV_NO_MANAGED_PYTHON": "false"},
                          {"UV_PYTHON_PREFERENCE": "only-managed"}):
            with self.subTest(selectors=selectors):
                self.assert_selected(self.run_selection(**selectors), "3.11")
                received = (self.home / "install-selectors").read_bytes()[:-1].split(b"\0")
                present = {key: value.decode() for key, value in zip(
                    ("UV_PYTHON", "UV_MANAGED_PYTHON", "UV_NO_MANAGED_PYTHON", "UV_PYTHON_PREFERENCE"), received
                ) if value != b"__unset__"}
                self.assertEqual(present, selectors)

    def test_forty_symlinks_are_allowed(self):
        base = self.external_base()
        previous = base
        links = self.root / "links"
        links.mkdir()
        for index in range(39):
            link = links / str(index)
            link.symlink_to(previous)
            previous = link
        self.runtime().symlink_to(previous)
        self.assert_selected(self.run_selection(), str(base))

    def test_more_than_forty_links_fail_before_install(self):
        previous = self.external_base()
        links = self.root / "links"
        links.mkdir()
        for index in range(40):
            link = links / str(index)
            link.symlink_to(previous)
            previous = link
        self.runtime().symlink_to(previous)
        result = self.run_selection()
        self.assert_selection_failed(result)
        self.assertIn("pass --python", result.stderr)

    def test_cycle_fails_before_install(self):
        runtime = self.runtime()
        other = runtime.parent / "other"
        runtime.symlink_to("other")
        other.symlink_to("python")
        self.assert_selection_failed(self.run_selection())

    def test_copied_executable_inside_existing_tool_fails_before_install(self):
        runtime = self.runtime()
        runtime.write_text("#!/bin/sh\nexit 0\n")
        runtime.chmod(0o755)
        self.assert_selection_failed(self.run_selection())

    def test_another_tools_executable_is_also_excluded(self):
        other = self.tools / "other-tool/bin/python"
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\nexit 0\n")
        other.chmod(0o755)
        self.runtime().symlink_to(other)
        self.assert_selection_failed(self.run_selection())

    def test_internal_runtime_through_tool_root_alias_is_excluded(self):
        runtime = self.runtime()
        other = self.tools / "other-tool/bin/python"
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\nexit 0\n")
        other.chmod(0o755)
        alias = self.root / "alias"
        alias.symlink_to(self.tools, target_is_directory=True)
        runtime.symlink_to(alias / "other-tool/bin/python")
        self.assert_selection_failed(self.run_selection())

    def test_broken_runtime_fails_before_install(self):
        self.runtime().symlink_to("missing-base")
        self.assert_selection_failed(self.run_selection())

    def test_existing_tool_without_runtime_fails_before_install(self):
        (self.tools / "claude-note").mkdir(parents=True)
        result = self.run_selection()
        self.assert_selection_failed(result)
        self.assertIn("has no Python", result.stderr)

    def test_nonexecutable_external_base_fails_before_install(self):
        base = self.external_base()
        base.chmod(0o644)
        self.runtime().symlink_to(base)
        self.assert_selection_failed(self.run_selection())

    def test_external_directory_is_not_an_interpreter(self):
        base = self.root / "directory"
        base.mkdir()
        self.runtime().symlink_to(base)
        self.assert_selection_failed(self.run_selection())

    def test_nonabsolute_tool_directory_fails_before_install(self):
        result = self.run_selection(FAKE_TOOL_ROOT="relative-tools")
        self.assert_selection_failed(result)
        self.assertIn("non-absolute", result.stderr)

    def test_tool_directory_error_does_not_fall_back_to_path_discovery(self):
        result = self.run_selection(FAKE_DIR_RC="43")
        self.assert_selection_failed(result)
        self.assertIn("could not locate", result.stderr)

    def test_link_target_newline_cannot_select_a_different_existing_base(self):
        ordinary = self.external_base()
        newline = self.external_base("external-base/bin/python\n")
        self.runtime().symlink_to(newline)
        self.assertTrue(ordinary.is_file())
        self.assert_selection_failed(self.run_selection())

    def test_tool_directory_trailing_newline_cannot_look_like_fresh_install(self):
        self.tools = self.root / "tools\n"
        base = self.external_base()
        self.runtime().symlink_to(base)
        self.assert_selection_failed(self.run_selection(UV_TOOL_DIR=str(self.tools)))

    def test_parent_alias_cannot_hide_newline_in_physical_base_directory(self):
        base = self.external_base("base\n/bin/python")
        alias = self.root / "ordinary-parent-alias"
        alias.symlink_to(base.parent, target_is_directory=True)
        self.runtime().symlink_to(alias / "python")
        self.assert_selection_failed(self.run_selection())

    def test_external_resolver_rejects_root_ownership_scope(self):
        base = self.external_base()
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c",
             'source "$1"; claude_note_external_python "$2" "$3"',
             "synthetic-selection", str(HELPER), str(base), "/"],
            cwd=self.root, env=self.environment(), capture_output=True, text=True, timeout=5,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.home / "uv-calls").exists())

    def test_selection_failure_preserves_config_and_completion_receipt(self):
        paths = [".config/claude-note/config.toml", ".local/share/claude-note/installed-from.json",
                 ".claude/settings.json", "refresh-wrapper.sh", "Library/LaunchAgents/refresh.plist"]
        before = {}
        for index, relative in enumerate(paths):
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"synthetic owner record {index}\n".encode())
            path.chmod(0o600)
            before[path] = (path.read_bytes(), path.stat().st_mode)
        self.runtime().symlink_to("missing-base")
        self.assert_selection_failed(self.run_selection())
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mode) for path in before}, before)

    def test_persistent_discovery_preference_does_not_migrate_existing_runtime(self):
        # This tests only our documented continuity policy. Fake uv does not
        # interpret configuration; actual UV preference semantics have their
        # own disposable contract experiment, independent of this harness.
        config = self.home / ".config/uv/uv.toml"
        config.parent.mkdir(parents=True)
        config.write_text('python-preference = "only-system"\n')
        before = config.read_bytes()
        base = self.external_base("managed-python/bin/python")
        self.runtime().symlink_to(base)
        self.assert_selected(self.run_selection(XDG_CONFIG_HOME=str(config.parent.parent)), str(base))
        self.assertEqual(config.read_bytes(), before)
        self.assert_selected(self.run_selection("3.11", XDG_CONFIG_HOME=str(config.parent.parent)), "3.11")


if __name__ == "__main__":
    unittest.main()
