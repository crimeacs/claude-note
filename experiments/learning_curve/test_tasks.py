"""Public task/verifier v2 contracts; no provider or application access."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("public_learning_tasks",HERE/"tasks.py")
tasks=importlib.util.module_from_spec(spec);spec.loader.exec_module(tasks)

class WorkflowVerifierContracts(unittest.TestCase):
    def action(self,task,queue):
        return {"request_id":task["task"]["request"]["id"],"queue":queue}

    def test_manual_expected_destinations_all_sixty(self):
        # Manually reasoned role answerkeys; independent of _expected implementation.
        a_first=("restricted_r1","general","erase","erase","erase","restricted_r1",
                 "restricted_r2","restricted_r1","restricted_r2","restricted_r1","erase","erase")
        b_first=("restricted_r1","general","erase","restricted_r1","restricted_r1","erase",
                 "restricted_r2","erase","restricted_r2","restricted_r1","erase","erase")
        for seed in range(5):
            config=tasks.policy_configuration(seed)
            for task,role in zip(tasks.trajectory(seed),a_first if seed%2==0 else b_first):
                for queue in task["allowed_queues"]:
                    with self.subTest(task=task["id"],queue=queue):
                        self.assertEqual(tasks.evaluate_action(task["id"],self.action(task,queue))["success"],
                                         queue==config[role])

    def test_twelve_episodes_five_independent_configurations(self):
        sequences=tasks.get_sequences()
        self.assertEqual(len(sequences),5)
        self.assertEqual(len({tuple(sorted(tasks.policy_configuration(i).items())) for i in range(5)}),5)
        all_tasks=[t for seq in sequences.values() for t in seq]
        self.assertEqual(len(all_tasks),60)
        self.assertEqual(len({t["id"] for t in all_tasks}),60)
        self.assertEqual(len({t["task"]["request"]["entity_id"] for t in all_tasks}),60)

    def test_new_procedures_follow_own_base_snapshot(self):
        for seed in range(5):
            seq=tasks.trajectory(seed);config=tasks.policy_configuration(seed)
            self.assertEqual({tasks._matched_procedure(t) for t in seq[:3]},
                             {"restricted-base","public-base","erase"})
            self.assertEqual({tasks._matched_procedure(seq[3]),tasks._matched_procedure(seq[5])},
                             {"legal-export","external-transfer"})
            self.assertTrue(all(t["allowed_queues"]==sorted(config.values()) for t in seq))
            self.assertEqual(tasks.curriculum(seed)["primary_transfer_episodes"],[5,8,9,10,12])
            self.assertEqual(tasks.curriculum(seed)["exposure_episodes"],[1,2,3,4,6,7,11])

    def test_A_B_exposure_order_is_counterbalanced_three_two(self):
        orders=[tasks.curriculum(seed)["order"] for seed in range(5)]
        self.assertEqual(orders.count("A-first"),3)
        self.assertEqual(orders.count("B-first"),2)
        for seed in range(5):
            seq=tasks.trajectory(seed)
            self.assertEqual(tasks._matched_procedure(seq[3]),tasks._matched_procedure(seq[4]))
            self.assertEqual(tasks._matched_procedure(seq[5]),tasks._matched_procedure(seq[7]))

    def test_transfer_changes_entity_and_visibility_or_operation(self):
        for seed in range(5):
            seq=tasks.trajectory(seed)
            for exposure,transfer in ((seq[3],seq[4]),(seq[5],seq[7]),(seq[10],seq[11])):
                a,b=exposure["task"]["request"],transfer["task"]["request"]
                self.assertNotEqual(a["entity_id"],b["entity_id"])
                self.assertTrue(a["operation"]!=b["operation"] or a["visibility"]!=b["visibility"])
                self.assertEqual(tasks._matched_procedure(exposure),tasks._matched_procedure(transfer))

    def test_corrected_base_route_changes_only_that_procedure(self):
        seq=tasks.trajectory(0);config=tasks.policy_configuration(0)
        old=tasks.score_episode(seq[8]["id"],[self.action(seq[8],config["restricted_r1"])])
        self.assertEqual(old["stale_route_count"],1)
        self.assertFalse(old["first_action_success"])
        self.assertTrue(tasks.evaluate_action(seq[8]["id"],self.action(seq[8],config["restricted_r2"]))["success"])

    def test_old_route_remains_valid_external_exception_after_correction(self):
        seq=tasks.trajectory(0);config=tasks.policy_configuration(0)
        score=tasks.score_episode(seq[9]["id"],[self.action(seq[9],config["restricted_r1"])])
        self.assertTrue(score["first_action_success"])
        self.assertEqual(score["stale_route_count"],0)
        over=tasks.score_episode(seq[9]["id"],[self.action(seq[9],config["restricted_r2"])])
        self.assertEqual(over["overscoped_correction_count"],1)
        self.assertFalse(over["first_action_success"])

    def test_erase_and_legal_export_priority_over_external_exception(self):
        config=tasks.policy_configuration(0);seq=tasks.trajectory(0)
        for task in (seq[2],seq[10],seq[11]):
            self.assertTrue(tasks.evaluate_action(task["id"],self.action(task,config["erase"]))["success"])
        self.assertEqual(tasks._matched_procedure(seq[10]),"legal-export")
        self.assertEqual(tasks._matched_procedure(seq[11]),"legal-export")
        self.assertEqual(seq[10]["task"]["request"]["region"],"external")
        self.assertFalse(tasks.evaluate_action(seq[10]["id"],self.action(seq[10],config["restricted_r1"]))["success"])

    def test_revision_change_announcement_does_not_export_mapping(self):
        seq=tasks.trajectory(0)
        self.assertTrue(seq[6]["announcement"])
        self.assertEqual(seq[6]["policy_revision"],"r2")
        self.assertFalse(any(q in seq[6]["announcement"] for q in seq[6]["allowed_queues"]))
        self.assertTrue(all(not t["announcement"] for t in seq if t["ordinal"]!=7))

    def test_backend_trace_contains_matched_CURRENT_fields_only(self):
        for seed in range(5):
            for task in tasks.trajectory(seed):
                result=tasks.evaluate_action(task["id"],self.action(task,task["allowed_queues"][0]))
                trace=result["feedback"]["routing_trace"]
                self.assertTrue(trace["matched_current_inputs"])
                request=task["task"]["request"]
                self.assertTrue(all(request[key]==value for key,value in trace["matched_current_inputs"].items()))
                self.assertNotIn("future",trace)
                self.assertNotIn("allowed_operations",trace)
                self.assertNotIn("routing_trace",tasks.public_view(task))

    def test_wrong_action_feedback_is_current_item_not_general_gold_lesson(self):
        task=tasks.trajectory(1)[0];config=tasks.policy_configuration(1)
        result=tasks.evaluate_action(task["id"],self.action(task,config["general"]))
        self.assertFalse(result["success"])
        self.assertEqual(result["feedback"]["required_queue"],config["restricted_r1"])
        self.assertEqual(result["feedback"]["request_id"],task["task"]["request"]["id"])
        self.assertFalse(any(w in json.dumps(result) for w in
                             ("restricted_r1","restricted_r2","policy_configuration","expected_role")))

    def test_correct_action_does_not_export_hidden_rule_map(self):
        task=tasks.trajectory(1)[0];config=tasks.policy_configuration(1)
        result=tasks.evaluate_action(task["id"],self.action(task,config["restricted_r1"]))
        self.assertTrue(result["success"])
        self.assertNotIn("required_queue",result["feedback"])
        self.assertEqual(set(result["feedback"]["routing_trace"]),
                         {"constraint_id","matched_current_inputs"})

    def test_invalid_actions_do_not_probe_answers_or_backend_trace(self):
        task=tasks.trajectory(2)[0];valid=self.action(task,task["allowed_queues"][0])
        for action in (None,[],{},{"request_id":valid["request_id"]},{**valid,"extra":"field"},
                       {**valid,"request_id":"wrong"},{**valid,"queue":"not-allowed"},{**valid,"queue":True}):
            with self.subTest(action=action):
                result=tasks.evaluate_action(task["id"],action)
                self.assertFalse(result["success"])
                self.assertEqual(result["feedback"]["observed_status"],"not_executed")
                self.assertNotIn("required_queue",result["feedback"])
                self.assertNotIn("routing_trace",result["feedback"])

    def test_two_actual_attempts_separate_first_and_final_success(self):
        task=tasks.trajectory(0)[8];config=tasks.policy_configuration(0)
        first=self.action(task,config["restricted_r1"])
        second=self.action(task,tasks.evaluate_action(task["id"],first)["feedback"]["required_queue"])
        score=tasks.score_episode(task["id"],[first,second])
        self.assertFalse(score["first_action_success"])
        self.assertTrue(score["final_task_success"])
        self.assertEqual(score["successful_action_index"],2)
        self.assertEqual(score["action_count"],2)

    def test_missing_provider_result_stays_unavailable(self):
        score=tasks.score_episode("lc-s00-e05",[])
        self.assertFalse(score["available"])
        self.assertIsNone(score["first_action_success"])
        self.assertIsNone(score["final_task_success"])
        self.assertEqual(score["action_count"],0)

    def test_action_cap_and_identity_bounds(self):
        task=tasks.trajectory(0)[0];action=self.action(task,task["allowed_queues"][0])
        for actions in (None,{},[action]*3):
            with self.assertRaises(ValueError):tasks.score_episode(task["id"],actions)
        for seed in (-1,5,True,"0"):
            with self.assertRaises(ValueError):tasks.trajectory(seed)
        for count in (0,6,True,"5"):
            with self.assertRaises(ValueError):tasks.get_sequences(count)
        for task_id in ("lc-s00-e13","lc-s05-e01",True,None):
            with self.assertRaises(ValueError):tasks.evaluate_action(task_id,action)

    def test_public_view_rejects_evaluator_or_mutated_fields(self):
        task=tasks.trajectory(0)[0]
        for changed in ({**task,"expected_queue":"Q-gold"},
                        {**task,"task":{**task["task"],"expected_answer":"gold"}},
                        {**task,"policy_revision":"r99"}):
            with self.assertRaises(ValueError):tasks.public_view(changed)

    def test_public_view_is_detached(self):
        task=tasks.trajectory(0)[9];view=tasks.public_view(task)
        view["allowed_queues"].append("Q-other")
        view["task"]["request"]["entity_id"]="changed"
        view["ambient_observations"][0]["text"]="changed"
        self.assertEqual(tasks.public_view(task),task)
        self.assertNotEqual(view,task)

    def test_interference_shared_bounded_cross_workflow_duplicates(self):
        seq=tasks.trajectory(0)
        self.assertTrue(all(not t["ambient_observations"] for t in seq[:9]))
        for task in seq[9:]:
            obs=task["ambient_observations"]
            self.assertEqual(len(obs),3)
            self.assertEqual(obs[0],obs[2])
            self.assertTrue(all(o["scope"]["workflow"]=="facilities" for o in obs))
            self.assertLess(len(json.dumps(obs)),1200)
            self.assertTrue(all(o["scope"]["company_id"]==task["scope"]["company_id"] for o in obs))

    def test_corpus_and_design_have_no_evaluator_answer_leakage(self):
        corpus=json.loads((HERE/"corpus.json").read_text())
        self.assertEqual(corpus["version"],2)
        self.assertEqual(corpus["sequences"],tasks.get_sequences())
        self.assertTrue(all(w not in json.dumps(corpus) for w in
                            ("expected_queue","restricted_r1","restricted_r2","policy_configuration",
                             "routing_trace","gold_lesson")))
        design=json.loads((HERE/"study_design.json").read_text())
        self.assertEqual(design["version"],2)
        self.assertEqual(design["arms"]["frozen"]["freeze_after_episode"],3)
        self.assertEqual(design["budgets"]["per_episode_actor_actions"],2)
        self.assertEqual(design["budgets"]["memory_context_characters"],4096)
        self.assertEqual(design["primary_analysis"]["episode_ordinals"],[5,8,9,10,12])

if __name__=="__main__":
    unittest.main()
