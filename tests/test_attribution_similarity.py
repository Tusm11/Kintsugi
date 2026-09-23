"""Tests for v2 attribution fingerprinting + similarity (src/attribution_similarity.py)."""

import pytest

from src.attribution_similarity import (
    AttributionSimilarity,
    MatchTier,
    calibration_report,
    categorize_evidence,
    evidence_shape,
    extract_features,
    fingerprint,
    normalize_cause,
)
from src.models import Attribution


def make_attr(cause, evidence_for=None, evidence_against=None, cf="pass", source="model"):
    return Attribution(
        claimed_cause=cause,
        evidence_for=list(evidence_for if evidence_for is not None else [
            "The diff changed the return statement",
            "Test expected 42 but got 41",
        ]),
        evidence_against=list(evidence_against or []),
        alternatives_considered=[{"cause": "flaky test", "why_rejected": "deterministic failure"}],
        counterfactual_result=cf,
        attribution_source=source,
    )


class TestNormalizeCause:
    def test_strips_identifiers_numbers_paths_and_line_refs(self):
        assert normalize_cause("Off-by-one in calculate_total() at line 42 of src/cart.py") == [
            "off", "one", "<id>", "<path>",
        ]

    def test_same_structure_different_names_normalize_equal(self):
        a = normalize_cause("Removed return statement in compute_price() in billing/price.py line 10")
        b = normalize_cause("Removed return statement in get_total_amount() in orders/total.py line 212")
        assert a == b

    def test_keeps_exception_class_names(self):
        tokens = normalize_cause("KeyError raised when user_id missing from cache_map")
        assert "keyerror" in tokens
        assert normalize_cause("TypeError raised when user_id missing") != normalize_cause(
            "KeyError raised when user_id missing"
        )

    def test_strips_model_labels_and_list_markers(self):
        assert normalize_cause("ROOT_CAUSE: Removed return in foo_bar()") == normalize_cause(
            "1. Removed return in foo_bar()"
        )

    def test_quoted_literals_collapse(self):
        assert normalize_cause("Wrong default 'utf-8' for encoding") == normalize_cause(
            'Wrong default "latin-1" for encoding'
        )

    def test_empty(self):
        assert normalize_cause("") == []
        assert normalize_cause(None) == []


class TestEvidenceShape:
    def test_placeholders_do_not_count(self):
        shape = evidence_shape(["No contradicting evidence found"])
        assert shape.count == 0
        assert categorize_evidence("Limited evidence available - model unavailable") == "placeholder"

    def test_categories(self):
        assert categorize_evidence("Test expected 42 but got 41") == "assertion"
        assert categorize_evidence("Index out of range on the last element") == "boundary"
        assert categorize_evidence("The commit removed a guard clause") == "diff"

    def test_shape_ignores_exact_text(self):
        a = evidence_shape(["Test expected 1 but got 2", "The diff changed foo"])
        b = evidence_shape(["expected 'a' got 'b'", "commit modified bar"])
        assert a == b


class TestFingerprint:
    def test_stable_across_instance_details(self):
        a = make_attr("Removed return statement in compute_price() line 10")
        b = make_attr("Removed return statement in total_for_order() line 99",
                      evidence_for=["commit changed the return", "expected 7 but got 8"])
        assert fingerprint(a) == fingerprint(b)

    def test_counterfactual_is_part_of_fingerprint(self):
        assert fingerprint(make_attr("Removed return", cf="pass")) != fingerprint(make_attr("Removed return", cf="fail"))

    def test_evidence_against_shape_is_part_of_fingerprint(self):
        a = make_attr("Removed return")
        b = make_attr("Removed return", evidence_against=["Logs show a timeout, not a logic error"])
        assert fingerprint(a) != fingerprint(b)


class TestTiering:
    sim = AttributionSimilarity()

    def test_exact_requires_identical_fingerprint(self):
        a = make_attr("Removed return statement in compute_price()")
        b = make_attr("Removed return statement in other_func()")
        assert self.sim.compare_attributions(a, b).tier == MatchTier.EXACT

        c = make_attr("Removed return statement in compute_price() before loop")
        result = self.sim.compare_attributions(a, c)
        assert result.tier != MatchTier.EXACT

    def test_near_for_similar_cause(self):
        a = make_attr("Removed return statement in compute_price() before loop")
        b = make_attr("Removed return statement in compute_price()")
        result = self.sim.compare_attributions(a, b)
        assert result.tier == MatchTier.NEAR
        assert result.score >= self.sim.near_threshold

    def test_none_for_unrelated_cause_even_with_same_evidence_shape(self):
        a = make_attr("Removed return statement in compute_price()")
        b = make_attr("Wrong timezone conversion for UTC timestamps")
        result = self.sim.compare_attributions(a, b)
        assert result.tier == MatchTier.NONE
        assert result.breakdown["cause"] < self.sim.min_cause_similarity

    def test_fallback_heuristic_capped_at_near(self):
        cached = make_attr("Removed return statement in f()")
        current = make_attr("Removed return statement in g()", source="fallback_heuristic")
        assert fingerprint(cached) == fingerprint(current)
        assert self.sim.compare_attributions(current, cached).tier == MatchTier.NEAR

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValueError):
            AttributionSimilarity(weights={"cause": 0.5, "evidence_for": 0.1, "evidence_against": 0.1, "counterfactual": 0.1})

    def test_best_match_prefers_exact_then_score(self):
        current = extract_features(make_attr("Removed return statement in compute_price()"))
        near = extract_features(make_attr("Removed return statement in compute_price() before loop"))
        exact = extract_features(make_attr("Removed return statement in other()"))
        payload, result = self.sim.best_match(current, [("near", near), ("exact", exact)])
        assert payload == "exact" and result.tier == MatchTier.EXACT


class TestCalibration:
    def test_no_false_exact_on_labeled_pairs(self):
        pairs = [
            (make_attr("Removed return in a_fn()"), make_attr("Removed return in b_fn()"), True),
            (make_attr("Off-by-one in loop over items_list"), make_attr("Off-by-one in loop over user_rows"), True),
            (make_attr("Removed return in a_fn()"), make_attr("Off-by-one in loop over items_list"), False),
            (make_attr("KeyError when config_key missing"), make_attr("TypeError when config_key missing"), False),
            (make_attr("Wrong timezone for UTC timestamps"), make_attr("Removed return in a_fn()"), False),
        ]
        report = calibration_report(pairs)
        assert report["false_exact"] == 0
        assert report["same_cause"].get("exact", 0) == 2
