"""Tests for v2 context-bucket tiering (src/context_buckets.py)."""

import pytest

from src.context_buckets import (
    ContextBucketBuilder,
    bucket_tier_for_attempt,
    focused_log_excerpt,
)
from src.models import Attribution, Run


def make_attr(cause="Removed return in compute_price()"):
    return Attribution(
        claimed_cause=cause,
        evidence_for=["diff removed return"],
        evidence_against=[],
        alternatives_considered=[{"cause": "flaky", "why_rejected": "deterministic"}],
        counterfactual_result="pass",
    )


def make_run(**kw):
    defaults = dict(
        repo="u/r",
        failure_logs="\n".join([f"noise line {i}" for i in range(50)] + ["E   AssertionError: expected 42 but got 41"]),
        diff="--- a/src/calc.py\n+++ b/src/calc.py\n- return 42\n+ return 41",
        commit_message="Tweak calc",
    )
    defaults.update(kw)
    return Run(**defaults)


def titles(bucket):
    return [t for t, _ in bucket.sections]


class TestTierMapping:
    def test_attempt_to_tier(self):
        assert [bucket_tier_for_attempt(i) for i in range(5)] == [1, 2, 3, 3, 3]


class TestContents:
    builder = ContextBucketBuilder()

    def test_bucket1_is_targeted(self):
        b = self.builder.build(1, make_run(), make_attr())
        assert titles(b) == ["Current diagnosis", "Failure log (focused excerpt)"]
        assert "noise line 0" not in b.render()
        assert "AssertionError" in b.render()

    def test_bucket1_seed_comes_first(self):
        b = self.builder.build(1, make_run(), make_attr(), seed_attribution=make_attr("seed cause"), seed_score=0.8)
        assert titles(b)[0].startswith("Prior verified diagnosis")
        assert b.seeded and b.seed_score == 0.8

    def test_bucket2_adds_diff_and_surrounding_code(self):
        run = make_run(metadata={"surrounding_code": "def neighbour(): ..."})
        b = self.builder.build(2, run, make_attr())
        assert "Recent change (diff)" in titles(b)
        assert "Surrounding code" in titles(b)

    def test_code_context_provider_takes_precedence_and_errors_are_swallowed(self):
        ok = ContextBucketBuilder(code_context_provider=lambda run: "def from_provider(): ...")
        assert "from_provider" in ok.build(2, make_run(), make_attr()).render()

        def boom(run):
            raise RuntimeError("checkout missing")

        broken = ContextBucketBuilder(code_context_provider=boom)
        assert "Surrounding code" not in titles(broken.build(2, make_run(), make_attr()))

    def test_bucket3_is_full(self):
        b = self.builder.build(3, make_run(), make_attr())
        text = b.render()
        assert "noise line 0" in text
        assert "Commit message" in titles(b)
        assert "Alternatives considered" in text
        assert b.char_budget is None

    def test_invalid_tier(self):
        with pytest.raises(ValueError):
            self.builder.build(4, make_run(), make_attr())


class TestBudgets:
    def test_rendered_size_within_budget(self):
        builder = ContextBucketBuilder(char_budgets={2: 400})
        run = make_run(diff="+" + "x" * 5000)
        b = builder.build(2, run, make_attr())
        assert b.char_count <= 400
        assert b.truncated

    def test_first_section_survives_tiny_budget(self):
        builder = ContextBucketBuilder(char_budgets={1: 40})
        b = builder.build(1, make_run(), make_attr())
        assert titles(b)[0] == "Current diagnosis"
        assert b.truncated and "Failure log (focused excerpt)" in b.dropped_sections

    def test_summary_has_no_content(self):
        b = ContextBucketBuilder().build(1, make_run(), make_attr())
        summary = b.summary()
        assert summary["tier"] == 1 and "sections" in summary
        assert "AssertionError" not in str(summary)


def test_focused_excerpt_falls_back_to_tail():
    logs = "\n".join(f"line {i}" for i in range(100))
    assert focused_log_excerpt(logs, max_lines=5).splitlines() == [f"line {i}" for i in range(95, 100)]
