"""Offline analyzer format/source gates; deterministic fixtures are not model data."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]
sys.path.insert(0,str(REPO/"src"));sys.path.insert(0,str(REPO/"tests"))
from support.public_own_learning import public_fixture

spec=importlib.util.spec_from_file_location("learning_analyzer_under_test",HERE/"analyze_study.py")
analyzer=importlib.util.module_from_spec(spec);spec.loader.exec_module(analyzer)

class AnalyzerRealArtifactControls(unittest.TestCase):
    def setUp(self):
        self.owned=self.enterContext(public_fixture())
        self.root=self.owned.root/"analysis-output"
        self.root.mkdir()
        self.module=importlib.import_module("claude_note.learning_loop")
        self.evaluator=analyzer.public_evaluator()
        self.audit=analyzer.ArtifactAudit(self.root)
        self.scope={"company_id":"company-00","workflow":"customer-records"}

    def make_real_capture(self):
        memory=self.root/"chains/company-00/learned/memory"
        task=self.evaluator.trajectory(0)[0]
        action={"request_id":task["task"]["request"]["id"],"queue":task["allowed_queues"][0]}
        outcome=self.evaluator.evaluate_action(task["id"],action)
        def deterministic_format_only(request):
            return {"lessons":[{"key":"format-control",
                "text":"Deterministic public artifact format control, not a model-learned lesson.",
                "source_spans":list(request["source_spans"]),"supersedes":[]}]}
        loop=self.module.LearningLoop(memory,run_id=analyzer.RUN_ID,arm_id="learned",
            extractor=deterministic_format_only,public_only=True,repository="public-learning-study",
            environment="public-study",scope=self.scope)
        try:
            receipt=loop.observe(task["id"],json.dumps(self.evaluator.public_view(task),sort_keys=True),
                json.dumps([action],sort_keys=True),{"attempts":[outcome]},scope=self.scope)
            result=loop.learn(receipt)
            self.assertEqual(result["status"],"completed",result)
            next_task=self.evaluator.trajectory(0)[1]
            view=loop.retrieve(analyzer.query_for(next_task),scope=self.scope)
        finally:loop.close()
        return memory,task,next_task,view

    def test_actual_loop_envelopes_capture_and_retrieval_are_compatible(self):
        memory,task,next_task,view=self.make_real_capture()
        raw=json.loads((memory/"observations/00000.json").read_text())
        self.assertEqual(set(raw),{"payload","sha256"})
        observed_memory,observations=analyzer.audit_observations(
            self.root,"company-00","learned",self.evaluator,self.audit)
        self.assertEqual(observed_memory,memory)
        self.assertEqual(len(observations),1)
        self.assertEqual(observations[0]["task_id"],task["id"])
        self.assertEqual(self.audit.loop_json(memory/"learning/00000.result.json")["status"],"completed")
        analyzer.audit_memory_before({"company":"company-00","arm":"learned","memory_before":view},
            next_task,memory,observations,self.audit,self.root)
        self.assertEqual(self.audit.issues,[])
        self.assertEqual(self.owned.calls["transport"],0)

    def test_actual_envelope_tamper_is_rejected_without_touching_original(self):
        memory,_,_,_=self.make_real_capture()
        raw=(memory/"observations/00000.json").read_bytes()
        envelope=json.loads(raw);envelope["payload"]["sequence"]=9
        tampered=self.root/"tampered-control.json"
        tampered.write_bytes(analyzer.encoded(envelope)+b"\n")
        with self.assertRaises(ValueError):self.audit.loop_json(tampered)
        self.assertEqual((memory/"observations/00000.json").read_bytes(),raw)
        self.assertEqual(self.owned.calls["transport"],0)

    def test_rewrapped_experience_cannot_differ_from_actual_capture_bytes(self):
        memory,_,_,_=self.make_real_capture()
        path=memory/"observations/00000.json"
        envelope=json.loads(path.read_bytes())
        envelope["payload"]["experience"]["verifier_feedback"]["control_note"]="not present in captured bytes"
        envelope["sha256"]=analyzer.digest(analyzer.encoded(envelope["payload"]))
        path.write_bytes(analyzer.encoded(envelope)+b"\n")
        analyzer.audit_observations(self.root,"company-00","learned",self.evaluator,self.audit)
        self.assertIn("capture_experience_bytes_or_anchors",[issue["code"] for issue in self.audit.issues])
        self.assertEqual(self.owned.calls["transport"],0)

    def test_noncanonical_unwrapped_extra_or_oversized_loop_record_is_rejected(self):
        payload={"sequence":0,"fixture":"public"}
        valid={"payload":payload,"sha256":analyzer.digest(analyzer.encoded(payload))}
        variants=[json.dumps(valid,indent=2).encode()+b"\n",
                  analyzer.encoded({**valid,"extra":True})+b"\n",
                  analyzer.encoded(payload)+b"\n",
                  b"x"*262145]
        for number,raw in enumerate(variants):
            path=self.root/("invalid-%d.json"%number);path.write_bytes(raw)
            with self.subTest(number=number),self.assertRaises(ValueError):self.audit.loop_json(path)

    def test_provider_and_cell_plain_json_are_not_unwrapped(self):
        envelope={"payload":{"fixture":"public"},"sha256":"not-a-loop-record"}
        path=self.root/"plain-provider-control.json"
        path.write_text(json.dumps(envelope))
        self.assertEqual(self.audit.json(path),envelope)
        with self.assertRaises(ValueError):self.audit.loop_json(path)

    def test_source_changes_suppress_all_comparative_statistics(self):
        files={"experiments/learning_curve/"+name:analyzer.digest((HERE/name).read_bytes())
               for name in ("tasks.py","corpus.json","study_design.json","analyze_study.py")}
        (self.root/"attempt.json").write_text(json.dumps({"source_commit":"authored-control-only","files":files}))
        (self.root/"predictions.json").write_text("[]")
        (self.root/"run-summary.json").write_text(json.dumps({"source_manifest_unchanged":False}))
        report=analyzer.analyze(self.root)
        self.assertFalse(report["scientific_comparisons"]["valid"])
        self.assertIsNone(report["paired_learned_minus_control"])
        self.assertTrue(report["descriptive_outcomes_only"])
        self.assertIn("run-source-manifest-changed",report["scientific_comparisons"]["reasons"])
        self.assertTrue(all(report["arms"][arm]["subsets"]["primary"]["first_action"]["unknown"]==25
                            for arm in analyzer.ARMS))
        self.assertEqual(self.owned.calls["transport"],0)

if __name__=="__main__":
    unittest.main()
