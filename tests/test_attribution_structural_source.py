"""
Unit tests for structural attribution_source tracking.

This test suite verifies that attribution_source is set STRUCTURALLY based on
whether provider.call_with_retry() succeeded, NOT by inspecting output text.

This prevents silent degradation where:
1. Real model output containing "Unable to determine" gets mislabeled as fallback
2. Fallback failures with different error messages get mislabeled as "model"
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from src.attribution import AttributionEngine
from src.models import Run, Budget, Attribution
from typing import Tuple


class TestStructuralSourceTracking:
    """Test that attribution_source is structural, not inferred from text"""

    @pytest.fixture
    def engine(self):
        """Create a fresh AttributionEngine (no repo checkouts configured)"""
        from src.sandbox import RepoSandbox
        return AttributionEngine(sandbox=RepoSandbox(repo_paths={}))

    @pytest.fixture
    def sample_run(self):
        """Create a minimal Run for testing"""
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            diff="--- a/src/file.py\n+++ b/src/file.py\n@@ -5,5 +5,5 @@\n-return session\n+return None",
            failure_logs="FAILED test_login - AssertionError: assert session is not None",
            budget=Budget()
        )
        return run

    def test_extract_suspected_causes_returns_tuple_with_model_source_on_success(self, engine):
        """When model call succeeds, return tuple with 'model' source"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            # Mock successful model call
            mock_provider = Mock()
            mock_response = Mock()
            mock_response.content = "Removed critical statement\nLogic error in condition"
            mock_provider.call_with_retry.return_value = (True, mock_response)  # success=True
            mock_get_provider.return_value = mock_provider

            causes, source = engine._extract_suspected_causes(
                diff="dummy diff",
                failure_logs="dummy logs"
            )

            # STRUCTURAL: source is "model" because API call succeeded
            assert source == "model"
            assert isinstance(causes, list)
            assert len(causes) > 0

    def test_extract_suspected_causes_returns_tuple_with_fallback_source_on_failure(self, engine):
        """When model call fails, return tuple with 'fallback_heuristic' source"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            # Mock failed model call
            mock_provider = Mock()
            mock_provider.call_with_retry.return_value = (False, None)  # success=False
            mock_get_provider.return_value = mock_provider

            causes, source = engine._extract_suspected_causes(
                diff="dummy diff",
                failure_logs="dummy logs"
            )

            # STRUCTURAL: source is "fallback_heuristic" because API call failed
            assert source == "fallback_heuristic"
            assert isinstance(causes, list)

    def test_gather_evidence_for_cause_returns_tuple_with_model_source_on_success(self, engine):
        """When model call succeeds, return tuple with 'model' source"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            mock_provider = Mock()
            mock_response = Mock()
            mock_response.content = "Code change aligns with failure\nLogs mention the changed function"
            mock_provider.call_with_retry.return_value = (True, mock_response)  # success=True
            mock_get_provider.return_value = mock_provider

            evidence, source = engine._gather_evidence_for_cause(
                cause="Removed return statement",
                diff="dummy diff",
                failure_logs="dummy logs",
                changed_files=["src/auth.py"]
            )

            # STRUCTURAL: source is "model" because API call succeeded
            assert source == "model"
            assert isinstance(evidence, list)

    def test_gather_evidence_for_cause_returns_tuple_with_fallback_source_on_failure(self, engine):
        """When model call fails, return tuple with 'fallback_heuristic' source"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            mock_provider = Mock()
            mock_provider.call_with_retry.return_value = (False, None)  # success=False
            mock_get_provider.return_value = mock_provider

            evidence, source = engine._gather_evidence_for_cause(
                cause="Removed return statement",
                diff="dummy diff",
                failure_logs="dummy logs",
                changed_files=["src/auth.py"]
            )

            # STRUCTURAL: source is "fallback_heuristic" because API call failed
            assert source == "fallback_heuristic"
            assert isinstance(evidence, list)

    def test_gather_evidence_against_cause_returns_tuple_with_model_source_on_success(self, engine):
        """When model call succeeds, return tuple with 'model' source"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            mock_provider = Mock()
            mock_response = Mock()
            mock_response.content = "Error appears unrelated to the change\nLogs show different issue"
            mock_provider.call_with_retry.return_value = (True, mock_response)  # success=True
            mock_get_provider.return_value = mock_provider

            evidence, source = engine._gather_evidence_against_cause(
                cause="Removed return statement",
                diff="dummy diff",
                failure_logs="dummy logs"
            )

            # STRUCTURAL: source is "model" because API call succeeded
            assert source == "model"
            assert isinstance(evidence, list)

    def test_gather_evidence_against_cause_returns_tuple_with_fallback_source_on_failure(self, engine):
        """When model call fails, return tuple with 'fallback_heuristic' source"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            mock_provider = Mock()
            mock_provider.call_with_retry.return_value = (False, None)  # success=False
            mock_get_provider.return_value = mock_provider

            evidence, source = engine._gather_evidence_against_cause(
                cause="Removed return statement",
                diff="dummy diff",
                failure_logs="dummy logs"
            )

            # STRUCTURAL: source is "fallback_heuristic" because API call failed
            assert source == "fallback_heuristic"
            assert isinstance(evidence, list)

    def test_counterfactual_is_executed_not_model_judged(self, engine, sample_run):
        """The counterfactual never calls a model; without a checkout it is inconclusive"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            result, source = engine._test_counterfactual(
                cause="Removed return statement",
                diff=sample_run.diff,
                run=sample_run
            )
            mock_get_provider.assert_not_called()
        assert source == "not_executed"
        assert result.counterfactual_outcome == "inconclusive"

    def test_attribute_sets_source_to_fallback_if_any_method_used_fallback(self, engine, sample_run):
        """
        Final attribution_source should be 'fallback_heuristic' if ANY method used fallback.
        This is the composite rule: trust only if ALL methods succeeded.
        """
        with patch('src.model_provider.get_provider') as mock_get_provider:
            mock_provider = Mock()
            
            # Simulate: extract_suspected_causes succeeds (model), but gather_evidence fails (fallback)
            def side_effect(prompt, budget_tokens, temperature):
                # First call: extract_suspected_causes → succeeds
                if "suspected root cause" in prompt.lower() or "suspected root cause" in prompt.lower() or "changes" in prompt.lower():
                    response = Mock()
                    response.content = "Removed return statement"
                    return (True, response)
                # Second call: gather_evidence → fails
                else:
                    return (False, None)
            
            mock_provider.call_with_retry.side_effect = side_effect
            mock_get_provider.return_value = mock_provider

            attribution = engine.attribute(sample_run)

            # Because gather_evidence failed (fallback), final source must be "fallback_heuristic"
            assert attribution.attribution_source == "fallback_heuristic"

    def test_attribute_sets_source_to_model_only_if_all_methods_succeed(self, engine, sample_run):
        """Final attribution_source should be 'model' only if ALL methods succeeded"""
        with patch('src.model_provider.get_provider') as mock_get_provider:
            mock_provider = Mock()
            mock_response = Mock()
            
            # All calls succeed
            mock_response.content = "Some model reasoning"
            mock_provider.call_with_retry.return_value = (True, mock_response)
            mock_get_provider.return_value = mock_provider

            attribution = engine.attribute(sample_run)

            # All calls succeeded, so final source is "model"
            assert attribution.attribution_source == "model"

    def test_no_string_inspection_for_source_detection(self, engine):
        """
        Verify that source detection does NOT inspect output text for keywords
        like "Unable to determine" or "model analysis failed".
        
        A successful API call (success=True) ALWAYS means source="model",
        even if response text contains those keywords.
        """
        with patch('src.model_provider.get_provider') as mock_get_provider:
            mock_provider = Mock()
            mock_response = Mock()
            
            # Response contains fallback-like keywords, but API call succeeded
            mock_response.content = "Unable to determine cause with certainty due to complex logic"
            mock_provider.call_with_retry.return_value = (True, mock_response)  # success=True
            mock_get_provider.return_value = mock_provider

            causes, source = engine._extract_suspected_causes(
                diff="dummy",
                failure_logs="dummy"
            )

            # STRUCTURAL check: success=True means source="model", REGARDLESS of text content
            assert source == "model"
            # The fact that text contains "Unable to determine" does NOT change this
            assert "Unable to determine" in mock_response.content
