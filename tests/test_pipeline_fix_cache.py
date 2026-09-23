"""End-to-end tests for the v2 semantic path: fix-cache + context buckets in the pipeline.

No network: the semantic/structural providers are recording mocks, and the
AttributionEngine (which builds its own provider from the environment) is
replaced with a stub that returns a fixed Attribution.
"""

from datetime import datetime
from itertools import count

import pytest

from src.fix_cache import FixCache, InMemoryFixCacheBackend
from src.ingestion import WebhookEvent
from src.model_provider import MockProvider
from src.models import Attribution, RunStatus, StepType
from src.pipeline import KintsugiPipeline


FIX_RESPONSE = (
    "ROOT_CAUSE: the return value was changed\n"
    "PROPOSED_FIX: return 42\n"
    "WHY_THIS_WORKS: restores the expected value\n"
    "RISK_ASSESSMENT: low"
)
LOGS = "FAILED tests/test_calc.py::test_total - AssertionError: expected 42 but got 41"
DIFF = "--- a/src/calc.py\n+++ b/src/calc.py\n- return 42\n+ return 41"

_ids = count()


class RecordingProvider(MockProvider):
    def __init__(self, response=FIX_RESPONSE):
        super().__init__(response=response, max_retries=0, base_wait_seconds=0)
        self.prompts = []

    def call(self, prompt, budget_tokens, temperature=0.7):
        self.prompts.append(prompt)
        return super().call(prompt, budget_tokens, temperature)


def make_attr(cause="Removed return statement in compute_total()"):
    return Attribution(
        claimed_cause=cause,
        evidence_for=["The diff changed the return statement", "Test expected 42 but got 41"],
        evidence_against=[],
        alternatives_considered=[{"cause": "flaky test", "why_rejected": "fails deterministically"}],
        counterfactual_result="pass",
        attribution_source="model",
    )


def make_event(logs=LOGS, diff=DIFF, repo="user/repo"):
    return WebhookEvent(
        source="github", repo=repo, commit=f"c{next(_ids)}", branch="main", build_id="b",
        failure_logs=logs, diff=diff, commit_message="Change calc",
        webhook_id=f"wh-{next(_ids)}", timestamp=datetime.utcnow(), metadata={},
    )


@pytest.fixture(autouse=True)
def default_budgets(monkeypatch):
    for name, value in [("MAX_RETRIES_MECHANICAL", "5"), ("MAX_RETRIES_STRUCTURAL", "3"),
                        ("MAX_RETRIES_SEMANTIC", "2"), ("MAX_RETRIES_TOTAL", "7")]:
        monkeypatch.setenv(name, value)


@pytest.fixture
def setup(monkeypatch):
    provider = RecordingProvider()
    cache = FixCache(backend=InMemoryFixCacheBackend())
    pipeline = KintsugiPipeline(semantic_provider=provider, structural_provider=RecordingProvider(), fix_cache=cache)

    state = {"attribution": make_attr(), "attribute_calls": 0}

    def fake_attribute(run):
        state["attribute_calls"] += 1
        return state["attribution"]

    monkeypatch.setattr(pipeline.attribution, "attribute", fake_attribute)
    stub_execution(pipeline, monkeypatch)
    return pipeline, provider, cache, state


def stub_execution(pipeline, monkeypatch):
    """Replace the two components that touch a real checkout / GitHub.

    Their real behaviour is covered in tests/test_sandbox.py; here we test the
    orchestration. The Action Layer stub still enforces should_apply_fix.
    """
    monkeypatch.setattr(pipeline.verifier, "verify", lambda run: (True, "All tests passed", {"tests_passed": 1}))

    def apply_fix(run):
        ok, reason = pipeline.action_layer.should_apply_fix(run)
        return (True, "https://github.test/pr/1") if ok else (False, reason)

    monkeypatch.setattr(pipeline.action_layer, "apply_fix", apply_fix)


def run_event(pipeline, event):
    run = pipeline.ingest_event(event)
    status, summary = pipeline.process_run(run)
    return run, status, summary


def repair_steps(run):
    return [s for s in run.steps if s.type == StepType.REPAIR]


class TestColdPath:
    def test_cold_run_heals_uses_bucket1_and_stores(self, setup):
        pipeline, provider, cache, state = setup
        run, status, _ = run_event(pipeline, make_event())
        assert status == RunStatus.HEALED
        assert len(provider.prompts) == 1 and "bucket 1 of 3" in provider.prompts[0]
        assert cache.metrics.counters["store"] == 1
        assert cache.metrics.counters["bucket.used.bucket_1"] == 1
        assert repair_steps(run)[0].input["context_bucket"]["tier"] == 1

    def test_pipeline_without_cache_still_uses_buckets(self, monkeypatch):
        provider = RecordingProvider()
        pipeline = KintsugiPipeline(semantic_provider=provider, structural_provider=RecordingProvider(), use_fix_cache=False)
        monkeypatch.setattr(pipeline.attribution, "attribute", lambda run: make_attr())
        stub_execution(pipeline, monkeypatch)
        _, status, _ = run_event(pipeline, make_event())
        assert status == RunStatus.HEALED and pipeline.fix_cache is None
        assert "bucket 1 of 3" in provider.prompts[0]


class TestExactReuse:
    def test_signature_hit_skips_attribution_and_generation(self, setup):
        pipeline, provider, cache, state = setup
        run_event(pipeline, make_event())
        calls_before, prompts_before = state["attribute_calls"], len(provider.prompts)

        run, status, summary = run_event(pipeline, make_event())
        assert status == RunStatus.HEALED
        assert state["attribute_calls"] == calls_before        # Attribution Engine skipped
        assert len(provider.prompts) == prompts_before         # generation skipped
        assert repair_steps(run)[0].output["repair_output"]["source"] == "fix_cache"
        assert cache.metrics.counters["reuse.verified_pass"] == 1

    def test_exact_reuse_still_runs_gates_and_verifier(self, setup, monkeypatch):
        pipeline, provider, cache, state = setup
        run_event(pipeline, make_event())
        calls = {"gate": 0, "scope": 0, "verify": 0}
        gate, scope, verify = (pipeline.confidence_gate.is_eligible_for_auto_apply,
                               pipeline.scope_guard.is_in_scope_for_auto_apply,
                               pipeline.verifier.verify_and_record)

        def wrap(name, fn):
            def inner(*a, **kw):
                calls[name] += 1
                return fn(*a, **kw)
            return inner

        monkeypatch.setattr(pipeline.confidence_gate, "is_eligible_for_auto_apply", wrap("gate", gate))
        monkeypatch.setattr(pipeline.scope_guard, "is_in_scope_for_auto_apply", wrap("scope", scope))
        monkeypatch.setattr(pipeline.verifier, "verify_and_record", wrap("verify", verify))

        _, status, _ = run_event(pipeline, make_event())
        assert status == RunStatus.HEALED
        assert calls == {"gate": 1, "scope": 1, "verify": 1}

    def test_fingerprint_exact_when_signature_differs(self, setup):
        pipeline, provider, cache, state = setup
        run_event(pipeline, make_event())
        prompts_before = len(provider.prompts)
        # Different logs -> different signature; same root cause -> same fingerprint.
        state["attribution"] = make_attr("Removed return statement in another_function()")
        run, status, _ = run_event(pipeline, make_event(logs="FAILED tests/test_other.py::test_x - ValueError: bad"))
        assert status == RunStatus.HEALED
        assert state["attribute_calls"] == 2                    # attribution ran
        assert len(provider.prompts) == prompts_before          # generation skipped
        assert repair_steps(run)[0].output["repair_output"]["cache_lookup_path"] == "attribution"

    def test_failed_reverification_busts_and_falls_back_to_generation(self, setup, monkeypatch):
        pipeline, provider, cache, state = setup
        run_event(pipeline, make_event())
        prompts_before = len(provider.prompts)

        outcomes = iter([False, True])  # cached patch fails, regenerated one passes

        def verify(run):
            return next(outcomes), "stubbed", {}

        monkeypatch.setattr(pipeline.verifier, "verify", verify)
        run, status, _ = run_event(pipeline, make_event())
        assert status == RunStatus.HEALED
        assert cache.metrics.counters["bust"] == 1
        assert cache.metrics.counters["reuse.verified_fail"] == 1
        assert len(provider.prompts) == prompts_before + 1      # regenerated
        sources = [s.output["repair_output"].get("source") for s in repair_steps(run)]
        assert sources == ["fix_cache", None]
        assert cache.metrics.counters["store"] == 2             # fresh verified fix re-cached

    def test_scope_failure_on_reuse_escalates_without_busting(self, setup):
        pipeline, provider, cache, state = setup
        run_event(pipeline, make_event())
        protected = "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n- a\n+ b"
        _, status, summary = run_event(pipeline, make_event(diff=protected))
        assert status == RunStatus.ESCALATED and "Scope guard" in summary
        assert cache.metrics.counters.get("bust", 0) == 0


class TestNearAndBuckets:
    def test_near_match_seeds_bucket1_and_never_applies_cached_patch(self, setup):
        pipeline, provider, cache, state = setup
        run_event(pipeline, make_event())
        state["attribution"] = make_attr("Removed return statement in compute_total() before loop")
        run, status, _ = run_event(pipeline, make_event(logs="FAILED tests/test_calc.py::test_loop - IndexError"))
        assert status == RunStatus.HEALED
        assert cache.metrics.counters["lookup.near"] == 1
        assert "Prior verified diagnosis" in provider.prompts[-1]
        step = repair_steps(run)[0]
        assert step.output["repair_output"].get("source") is None  # generated, not reused
        assert step.input["context_bucket"]["seeded"] is True

    def test_verification_failure_widens_to_bucket2_then_escalates(self, setup, monkeypatch):
        pipeline, provider, cache, state = setup
        monkeypatch.setattr(pipeline.verifier, "verify", lambda run: (False, "tests failed", {}))
        run, status, summary = run_event(pipeline, make_event())
        assert status == RunStatus.ESCALATED
        assert ["bucket 1 of 3" in provider.prompts[0], "bucket 2 of 3" in provider.prompts[1]] == [True, True]
        assert len(provider.prompts) == 2                       # MAX_RETRIES_SEMANTIC=2
        assert "Verification failed" in summary
        assert cache.metrics.counters.get("store", 0) == 0      # nothing unverified is cached

    def test_bucket3_reached_when_semantic_budget_is_3(self, setup, monkeypatch):
        monkeypatch.setenv("MAX_RETRIES_SEMANTIC", "3")
        pipeline, provider, cache, state = setup
        monkeypatch.setattr(pipeline.verifier, "verify", lambda run: (False, "tests failed", {}))
        run_event(pipeline, make_event())
        assert [p.split("Context (bucket ")[1][0] for p in provider.prompts] == ["1", "2", "3"]

    def test_output_guardrail_rejection_is_not_retried(self, setup):
        pipeline, provider, cache, state = setup
        provider.response = FIX_RESPONSE.replace("return 42", "eval(user_input)")
        _, status, summary = run_event(pipeline, make_event())
        assert status == RunStatus.ESCALATED and summary.startswith("Output guardrail")
        assert len(provider.prompts) == 1
