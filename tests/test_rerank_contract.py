"""Configured-model observations, not placeholder scores, may reorder evidence."""

import copy
import time
import unittest
from unittest import mock

from claude_note import recall


MODEL = "synthetic:public-ranker-v1"


class RerankExecutionContractTests(unittest.TestCase):
    def response(self):
        return {"rerank_status": "scored", "coverage": "exact_passage", "model": MODEL,
                "manifest": {"rerank_ready": True, "models": {"rerank": {"id": MODEL, "ready": True}}},
                "passage_scores": {"p0": .1, "p1": .9}, "scored_passage_ids": ["p0", "p1"]}

    def rank(self, response, *, manifest=None, ids=("p0", "p1", "tail")):
        hits = [recall.Hit(uri=f"qmd://public/{i}.md", title=str(i), snippet="Public bounded evidence",
                           scored_text="Public bounded evidence", revision="a" * 64, passage_id=identity)
                for i, identity in enumerate(ids)]
        settings = recall.Settings(engine="server", rerank_top=2, rerank_blend=0, log=False)
        result = recall.Result(status="hit", engine="server", manifest=manifest or {})
        with mock.patch.object(recall, "server_post", return_value=response):
            ranked = recall._rank_passages("public retry evidence", hits, settings, result, time.monotonic() + 1)
        self.assertEqual([h.passage_id for h in hits], list(ids), "Never mutate the caller's candidate order")
        self.assertEqual(result.server_calls[0]["route"], "/rerank")
        return [h.passage_id for h in ranked], result

    def reject(self, response, coverage, **kwargs):
        order, result = self.rank(response, **kwargs)
        self.assertEqual(order, list(kwargs.get("ids", ("p0", "p1", "tail"))))
        self.assertFalse(result.reranked)
        self.assertEqual(result.coverage["rerank"], coverage)
        return result

    def test_complete_configured_model_reorders_only_ranked_prefix(self):
        order, result = self.rank(self.response(), manifest={"models": {"rerank": {"id": MODEL}}})
        self.assertEqual(order, ["p1", "p0", "tail"])
        self.assertTrue(result.reranked)
        self.assertEqual(result.coverage["rerank"], "exact_passage")

    def test_legitimate_half_score_is_not_fallback(self):
        response = self.response()
        response["passage_scores"]["p0"] = .5
        order, result = self.rank(response)
        self.assertEqual(order, ["p1", "p0", "tail"])
        self.assertTrue(result.reranked)

    def test_fallback_with_complete_scores_keeps_fusion_order(self):
        response = self.response()
        response.update(model="fallback", rerank_status="sdk_fallback", coverage="sdk_fallback_no_rerank")
        response["passage_scores"] = {"p0": .5, "p1": .5}
        self.reject(response, "sdk_fallback_no_rerank")

    def test_each_fallback_discriminator_overrides_conflicting_success(self):
        for key, value in [("model", "fallback"), ("rerank_status", "sdk_fallback"),
                           ("coverage", "sdk_fallback_no_rerank")]:
            with self.subTest(key=key):
                response = self.response()
                response[key] = value
                self.reject(response, "sdk_fallback_no_rerank")

    def test_legacy_scores_without_execution_contract_are_unverified(self):
        response = self.response()
        for key in ("rerank_status", "coverage", "model", "manifest"):
            response.pop(key)
        self.reject(response, "unverified_rerank_execution")

    def test_missing_wrong_reserved_or_malformed_model_is_unverified(self):
        for model in (None, "synthetic:wrong", "unknown", "", [], {"id": MODEL}, MODEL + "\n", "\ud800", "x" * 1025):
            with self.subTest(model=repr(model)):
                response = self.response()
                response["model"] = model
                self.reject(response, "unverified_model")

    def test_missing_or_mismatched_manifest_model_is_unverified(self):
        for manifest in (None, [], {}, {"models": []}, {"models": {"rerank": None}},
                         {"rerank_ready": True, "models": {"rerank": {"id": "other", "ready": True}}}):
            with self.subTest(manifest=manifest):
                response = self.response()
                response["manifest"] = manifest
                self.reject(response, "unverified_model")

    def test_model_drift_since_search_is_unverified(self):
        self.reject(self.response(), "unverified_model", manifest={"models": {"rerank": {"id": "previous:model"}}})

    def test_success_requires_both_boolean_readiness_observations(self):
        for location in ("manifest", "model"):
            for value in (False, None, 1, "true"):
                with self.subTest(location=location, value=value):
                    response = self.response()
                    target = response["manifest"] if location == "manifest" else response["manifest"]["models"]["rerank"]
                    target["rerank_ready" if location == "manifest" else "ready"] = value
                    self.reject(response, "unverified_model")

    def test_known_degraded_status_cannot_admit_complete_scores(self):
        pairs = [("partial_scores", "partial_passage_scores"), ("invalid_scores", "invalid_scores"),
                 ("no_valid_scores", "no_valid_scores"), ("unverified_model", "unverified_model"),
                 ("model_cache_unavailable", "model_cache_unavailable"), ("not_requested", "no_passages")]
        for status, coverage in pairs:
            with self.subTest(status=status):
                response = self.response()
                response.update(rerank_status=status, coverage=coverage)
                self.reject(response, coverage)

    def test_unknown_or_contradictory_status_is_bounded_and_unverified(self):
        for status, coverage in [("scored", "partial_passage_scores"), ("partial_scores", "exact_passage"),
                                 ("private source text", "private source text"), ("unknown", None), ([], {}), (None, None)]:
            with self.subTest(status=status):
                response = self.response()
                response.update(rerank_status=status, coverage=coverage)
                result = self.reject(response, "unverified_rerank_execution")
                self.assertNotIn("private source text", str(result.to_dict()))

    def test_nonfinite_boolean_string_and_oversized_integer_scores_are_invalid(self):
        for value in (float("nan"), float("inf"), -float("inf"), True, "0.9", None, [], 10 ** 1000):
            with self.subTest(value=repr(value)[:80]):
                response = self.response()
                response["passage_scores"]["p1"] = value
                self.reject(response, "invalid_scores")

    def test_missing_extra_or_duplicate_passage_id_is_invalid(self):
        for confirmed in (["p0"], ["p0", "p1", "foreign"], ["p0", "p0"], ["p0", {}], "p0,p1"):
            with self.subTest(confirmed=confirmed):
                response = self.response()
                response["scored_passage_ids"] = confirmed
                self.reject(response, "invalid_scores")

    def test_score_map_must_exactly_cover_requested_passages(self):
        for scores in ({}, {"p0": .9}, {"p0": .1, "p1": .9, "foreign": 1}, {"foreign": .9}, [], None):
            with self.subTest(scores=scores):
                response = self.response()
                response["passage_scores"] = scores
                self.reject(response, "invalid_scores")

    def test_duplicate_request_identity_is_not_an_exact_scoring_contract(self):
        self.reject(self.response(), "invalid_scores", ids=("p0", "p0", "tail"))

    def test_error_with_positive_contract_cannot_reorder(self):
        response = self.response()
        response["error"] = "deadline"
        self.reject(response, "unavailable_or_unverified_passage")

    def test_success_and_fallback_do_not_leak_across_independent_results(self):
        original = self.response()
        fallback = copy.deepcopy(original)
        fallback.update(model="fallback", rerank_status="sdk_fallback", coverage="sdk_fallback_no_rerank")
        self.assertTrue(self.rank(original)[1].reranked)
        self.reject(fallback, "sdk_fallback_no_rerank")
        self.assertTrue(self.rank(original)[1].reranked)


if __name__ == "__main__":
    unittest.main()
