"""
Integration test for attribution engine with real model provider.

To run this test with real Groq:
1. Set GROQ_API_KEY=<your-key> in .env
2. Set RUN_REAL_MODEL_TESTS=true in .env
3. Run: pytest tests/test_attribution_real_model.py -v

This test verifies:
- Real model calls are made (not mocked)
- Attribution object has correct source tracking
- Evidence is populated from model reasoning
- Counterfactual reasoning works end-to-end
- Confidence Gate properly evaluates model-sourced vs fallback attributions
"""

import os
import pytest
from datetime import datetime

from src.models import Run, Budget, RunStatus
from src.attribution import AttributionEngine
from src.guardrails import ConfidenceGate
from src.ingestion import WebhookEvent


@pytest.fixture
def skip_if_no_real_tests():
    """Skip test if real model tests are disabled or no API key"""
    if not os.getenv("RUN_REAL_MODEL_TESTS") == "true":
        pytest.skip("RUN_REAL_MODEL_TESTS not enabled")
    if not os.getenv("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY not set")


@pytest.fixture
def sample_failing_run():
    """Create a realistic failing Run for testing"""
    run = Run(
        repo="user/project",
        failing_commit="abc123def",
        failure_logs="""
        FAILED tests/test_auth.py::test_login_creates_session - AssertionError: assert False
        
        Expected session to be created after successful login
        Got None for session object
        
        Stack trace:
        File tests/test_auth.py, line 42, in test_login_creates_session
          assert user.session is not None
        """,
        diff="""
        --- a/src/auth.py
        +++ b/src/auth.py
        @@ -10,7 +10,7 @@ def login(username, password):
             if authenticate(username, password):
        -        session = create_session(user_id=user.id)
        -        return session
        +        # TODO: implement session creation
        +        return None
         else:
             raise AuthenticationError()
        """,
        commit_message="WIP: refactoring session management",
        budget=Budget(
            max_tokens=5000,
            max_retries_mechanical=5,
            max_retries_structural=3,
            max_retries_semantic=2,
            max_retries_total=7
        )
    )
    return run


class TestAttributionWithRealModel:
    """Integration tests using real model provider"""
    
    def test_attribution_creates_model_sourced_attribution(self, skip_if_no_real_tests, sample_failing_run):
        """Test that real model call produces model-sourced attribution"""
        engine = AttributionEngine()
        run = sample_failing_run
        
        # Perform attribution with real model
        attribution = engine.attribute(run)
        
        # Verify attribution structure
        assert attribution.claimed_cause, "Should identify a root cause"
        assert len(attribution.evidence_for) > 0, "Should gather supporting evidence from model"
        assert attribution.counterfactual_result in ["pass", "fail", "inconclusive"], "Should have valid counterfactual outcome"
        
        # Key verification: attribution_source should be "model", not "fallback_heuristic"
        assert attribution.attribution_source == "model", f"Expected model-sourced attribution, got {attribution.attribution_source}"
        
        # Verify no false precision
        assert attribution.evidence_strength_source == "n/a", "Should not use strength scores"
        
        print(f"\n✓ Real Attribution:")
        print(f"  Cause: {attribution.claimed_cause}")
        print(f"  Evidence For: {attribution.evidence_for[:2]}")  # First 2
        print(f"  Counterfactual: {attribution.counterfactual_result}")
        print(f"  Source: {attribution.attribution_source}")
    
    def test_confidence_gate_accepts_model_sourced(self, skip_if_no_real_tests, sample_failing_run):
        """Test that Confidence Gate accepts model-sourced attribution"""
        engine = AttributionEngine()
        gate = ConfidenceGate()
        run = sample_failing_run
        
        # Add attribution to run
        attribution = engine.attribute(run)
        step = engine.create_attribution_step(run, attribution)
        run.add_step(step)
        
        # Only test gate if we got a passing counterfactual
        if attribution.counterfactual_result == "pass" and len(attribution.evidence_against) == 0:
            eligible, reason = gate.is_eligible_for_auto_apply(run)
            print(f"\n✓ Gate Decision: {eligible}")
            print(f"  Reason: {reason}")
            assert "model-sourced" in reason or eligible, "Should indicate model-sourced in decision"
    
    def test_confidence_gate_rejects_fallback_sourced(self, sample_failing_run):
        """Test that Confidence Gate explicitly rejects fallback-sourced attribution"""
        from src.models import Attribution, Step, StepType, StepLayer, StepStatus
        
        gate = ConfidenceGate()
        run = sample_failing_run
        
        # Create a fallback-sourced attribution manually
        fallback_attribution = Attribution(
            claimed_cause="Unable to determine cause - model analysis failed",
            evidence_for=["Pattern matches removed code"],
            evidence_against=[],
            alternatives_considered=[],
            counterfactual_result="pass",  # Even with perfect structure...
            attribution_source="fallback_heuristic"  # ...fallback source
        )
        
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            attribution=fallback_attribution
        )
        run.add_step(step)
        
        # Gate should reject it
        eligible, reason = gate.is_eligible_for_auto_apply(run)
        assert not eligible, "Should reject fallback-sourced attribution"
        assert "fallback heuristic" in reason.lower(), f"Reason should mention fallback source, got: {reason}"
        
        print(f"\n✓ Gate Correctly Rejected Fallback:")
        print(f"  Reason: {reason}")
    
    def test_real_model_output_format(self, skip_if_no_real_tests, sample_failing_run):
        """Verify real model output is properly parsed into Attribution structure"""
        engine = AttributionEngine()
        run = sample_failing_run
        
        attribution = engine.attribute(run)
        
        # Verify each field is populated correctly
        assert isinstance(attribution.claimed_cause, str) and attribution.claimed_cause, "Cause should be non-empty string"
        assert isinstance(attribution.evidence_for, list), "evidence_for should be list"
        assert isinstance(attribution.evidence_against, list), "evidence_against should be list"
        assert isinstance(attribution.alternatives_considered, list), "alternatives should be list"
        assert attribution.counterfactual_result in ["pass", "fail", "inconclusive"], "Counterfactual should have valid value"
        assert attribution.attribution_source in ["model", "fallback_heuristic"], "Source should be valid"
        
        # Evidence items should be strings
        for evidence in attribution.evidence_for:
            assert isinstance(evidence, str) and len(evidence) > 0, f"Evidence should be non-empty string, got {evidence}"
        
        print(f"\n✓ Output Format Validated:")
        print(f"  Claimed Cause: {type(attribution.claimed_cause).__name__} ✓")
        print(f"  Evidence For: {len(attribution.evidence_for)} items ✓")
        print(f"  Evidence Against: {len(attribution.evidence_against)} items ✓")
        print(f"  Alternatives: {len(attribution.alternatives_considered)} items ✓")
        print(f"  Counterfactual Result: {attribution.counterfactual_result} ✓")
        print(f"  Source Tracking: {attribution.attribution_source} ✓")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
