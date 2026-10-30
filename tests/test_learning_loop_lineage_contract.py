"""Independent regression witnesses for real supplied-memory provenance gaps.

Frozen separately from the first 26 feature contracts. Only public temporary
fixtures and an injected Python extractor execute. These assertions are genuine
compatible behavior requirements against the initial implemented loop, not a
missing-module RED. No provider, native retrieval or production fixture access.
"""
import importlib
from pathlib import Path
import sys
import unittest

# The external trusted harness reconstructs the exact source slice from Git and
# uses -I. Select that reconstructed source, never an ambient installed package.
REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY/"src"))
sys.path.insert(0, str(REPOSITORY/"tests"))
from support.public_own_learning import public_fixture


class LearningLoopLineageRegressionTests(unittest.TestCase):
    def setUp(self):
        self.owned = self.enterContext(public_fixture())
        self.module = importlib.import_module("claude_note.learning_loop")
        self.root = self.owned.root/"lineage-loop"
        self.requests = []
        self.loop = None

    def tearDown(self):
        if self.loop is not None:
            self.loop.close()
        self.assertEqual(self.owned.calls["transport"], 0)

    def create(self, extractor):
        self.loop = self.module.LearningLoop(self.root, run_id="public-lineage-run",
            arm_id="learning", extractor=extractor, public_only=True,
            scope={"company_id": "public-company-a", "workflow": "retry"})
        return self.loop

    @staticmethod
    def proposal(request):
        return {"lessons": [{"key": "retry", "text": request["experience"]["actual_response"],
                             "source_spans": list(request["source_spans"])}]}

    def observed(self, sequence, response):
        return self.loop.observe("task-"+str(sequence), "Public retry transfer task.",
                                 response, {"passed": True, "fixture": "public-only"})

    def entry(self):
        result = self.loop.retrieve("retry")
        self.assertEqual(len(result["entries"]), 1)
        return result["entries"][0]

    def test_consolidation_records_current_capture_and_supplied_prior_revision(self):
        def extractor(request):
            self.requests.append(request)
            return self.proposal(request)
        self.create(extractor)
        first = self.observed(0, "Keep the retry key.")
        self.assertEqual(self.loop.learn(first)["status"], "completed")
        prior = self.entry()
        prior_path = self.root/prior["relative_path"]
        prior_bytes = prior_path.read_bytes()
        second = self.observed(1, "Keep the retry key and compare its payload.")
        self.assertEqual(self.loop.learn(second)["status"], "completed")
        self.assertEqual(self.requests[1]["current_memory"][0]["revision"], prior["revision"])
        learned = self.root/self.entry()["relative_path"]
        current_capture = Path(second["source_spans"][0].rsplit("#L", 1)[0])
        authored = self.owned.app.egress.derivations(learned.read_text())
        self.assertIn(str(prior_path), authored,
            "Consolidation must retain the supplied prior learned revision as a derivation source")
        self.assertIn(str(current_capture), authored,
            "Consolidation must retain the actual current captured source")
        recorded = self.owned.app.egress.recorded_derivations(learned)
        self.assertIn(str(prior_path), recorded,
            "The real restrictive lineage ledger must retain supplied prior memory")
        self.assertIn(str(current_capture), recorded)
        self.assertEqual(prior_path.read_bytes(), prior_bytes)

    def test_prior_memory_changed_during_extraction_cannot_complete_learning(self):
        changes = []
        def extractor(request):
            if request["current_memory"]:
                supplied = request["current_memory"][0]
                target = self.root/supplied["relative_path"]
                target.write_bytes(target.read_bytes()+b"\nPUBLIC-PRIOR-TAMPER\n")
                changes.append(supplied["revision"])
            return self.proposal(request)
        self.create(extractor)
        first = self.observed(0, "Keep the retry key.")
        self.assertEqual(self.loop.learn(first)["status"], "completed")
        second = self.observed(1, "Compare the retry payload.")
        result = self.loop.learn(second)
        self.assertEqual(len(changes), 1)
        self.assertNotEqual(result["status"], "completed",
            "Changed supplied prior memory must prevent completed learning")

    def test_ancestor_symlink_root_is_refused_before_outside_creation(self):
        outside = self.owned.root/"separate-public-directory"
        outside.mkdir()
        alias = self.owned.root/"public-root-alias"
        alias.symlink_to(outside, target_is_directory=True)
        self.root = alias/"new-loop"
        with self.assertRaises(self.module.LearningLoopError,
                               msg="Every supplied root ancestor must be unlinked"):
            self.create(self.proposal)
        self.assertFalse((outside/"new-loop").exists(),
                         "A refused root must not create files through an ancestor link")

    def test_replaced_lock_inode_refuses_new_effects_and_extractor_entry(self):
        def extractor(request):
            self.requests.append(request)
            return self.proposal(request)
        self.create(extractor)
        observed = self.observed(0, "Original public capture.")
        captures_before = set((self.root/"captures").glob("*"))
        lock_path = self.root/".learning-loop.lock"
        lock_path.unlink()
        lock_path.write_bytes(b"PUBLIC-REPLACEMENT-LOCK\n")
        with self.assertRaises(self.module.LearningLoopError,
                               msg="A replaced lock inode must block new observation effects"):
            self.observed(1, "This capture must not be published through a retired lock.")
        with self.assertRaises(self.module.LearningLoopError,
                               msg="A replaced lock inode must block extractor entry"):
            self.loop.learn(observed)
        self.assertEqual(self.requests, [])
        self.assertEqual(set((self.root/"captures").glob("*")), captures_before)


if __name__ == "__main__":
    unittest.main()
