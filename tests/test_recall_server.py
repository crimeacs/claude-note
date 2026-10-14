"""Run the server's public-fixture, queue and exact-passage contracts with Node."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class RecallServerTests(unittest.TestCase):
    def test_server_contracts(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the optional recall server")
        env = dict(os.environ)
        qmd = shutil.which("qmd")
        if qmd:
            sdk = Path(qmd).resolve().parent.parent / "dist/index.js"
            if sdk.exists():
                env["CLAUDE_NOTE_TEST_QMD_SDK"] = str(sdk)
                found = subprocess.run([node, "--input-type=module", "-e",
                    "import {createRequire} from 'node:module'; console.log(createRequire(process.argv[1]).resolve('better-sqlite3'));", str(sdk)],
                    capture_output=True, text=True, timeout=10)
                if found.returncode == 0:
                    env["CLAUDE_NOTE_TEST_SQLITE"] = found.stdout.strip()
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, "--test", str(root / "tests/recall-server.test.mjs")], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
