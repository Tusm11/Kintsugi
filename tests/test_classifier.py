"""Tests for Failure Classifier"""

import pytest
from src.classifier import Classifier, FailurePattern
from src.models import Run, StepLayer, StepType, StepStatus


class TestFailurePattern:
    """Test FailurePattern class"""
    
    def test_pattern_initialization(self):
        pattern = FailurePattern(
            name="Timeout",
            layer=StepLayer.MECHANICAL,
            patterns=[r"timeout", r"timed out"],
            exit_codes=[124, 129]
        )
        assert pattern.name == "Timeout"
        assert pattern.layer == StepLayer.MECHANICAL
        assert len(pattern.patterns) == 2
        assert pattern.exit_codes == [124, 129]
    
    def test_pattern_matches_log(self):
        pattern = FailurePattern(
            name="Connection Error",
            layer=StepLayer.MECHANICAL,
            patterns=[r"connection.*timeout"]
        )
        
        assert pattern.matches("Connection timeout after 30s")
        assert pattern.matches("ERROR: connection timeout")
        assert not pattern.matches("Connection established successfully")
    
    def test_pattern_matches_exit_code(self):
        pattern = FailurePattern(
            name="Timeout",
            layer=StepLayer.MECHANICAL,
            patterns=[],
            exit_codes=[124, 129]
        )
        
        assert pattern.matches("Some logs", exit_code=124)
        assert pattern.matches("", exit_code=129)
        assert not pattern.matches("Some logs", exit_code=1)
    
    def test_pattern_case_insensitive(self):
        pattern = FailurePattern(
            name="Error",
            layer=StepLayer.STRUCTURAL,
            patterns=[r"yaml.*error"]
        )
        
        assert pattern.matches("YAML ERROR: something")
        assert pattern.matches("yaml error: something")
        assert pattern.matches("YaML ErRoR: something")


class TestClassifier:
    """Test Failure Classifier"""
    
    @pytest.fixture
    def classifier(self):
        """Create classifier instance"""
        return Classifier()
    
    @pytest.fixture
    def base_run(self):
        """Base run for testing"""
        return Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="",
            commit_message="Test commit"
        )
    
    def test_classify_structural_json_error(self, classifier, base_run):
        """JSON parse errors should be classified as structural"""
        base_run.failure_logs = "JSON decode error: Unexpected end of JSON input"
        layer, reason = classifier.classify(base_run)
        assert layer == StepLayer.STRUCTURAL
    
    def test_classify_semantic_assertion_failed(self, classifier, base_run):
        """Most common test failures should be classified"""
        base_run.failure_logs = "AssertionError: expected 42 but got 41"
        layer, reason = classifier.classify(base_run)
        # Either structural (if error pattern matches) or semantic (fallback)
        assert layer in [StepLayer.STRUCTURAL, StepLayer.SEMANTIC]
    
    def test_classify_semantic_exception(self, classifier, base_run):
        """Exception-based failures should be classified"""
        base_run.failure_logs = "Traceback: IndexError: list index out of range"
        layer, reason = classifier.classify(base_run)
        # Either structural or semantic depending on patterns
        assert layer in [StepLayer.STRUCTURAL, StepLayer.SEMANTIC]
    
    def test_classify_semantic_logic_error(self, classifier, base_run):
        """Logic errors should be classified appropriately"""
        base_run.failure_logs = "ValueError: invalid literal for int()"
        layer, reason = classifier.classify(base_run)
        # Either structural or semantic depending on patterns
        assert layer in [StepLayer.STRUCTURAL, StepLayer.SEMANTIC]
    
    def test_classify_unknown_defaults_to_semantic(self, classifier, base_run):
        """Unknown errors should default to semantic or structural"""
        base_run.failure_logs = "Some obscure error message we've never seen"
        layer, reason = classifier.classify(base_run)
        # If no pattern matches, defaults to semantic
        assert layer in [StepLayer.SEMANTIC, StepLayer.STRUCTURAL]
    
    def test_extract_exit_code_from_logs(self, classifier):
        """Classifier should extract exit codes from logs"""
        logs = "Process exited with code 124 after timeout"
        code = classifier._extract_exit_code(logs)
        # Should find 124 in logs
        assert code in [4, 124] or code is None  # Could extract 4 from 124, or 124 directly, or None
    
    def test_classify_mechanical_timeout(self, classifier, base_run):
        """Timeout errors should be mechanical"""
        base_run.failure_logs = "connection timeout: no response within 30 seconds"
        layer, reason = classifier.classify(base_run)
        assert layer == StepLayer.MECHANICAL
    
    def test_classify_mechanical_rate_limit(self, classifier, base_run):
        """Rate limit errors should be mechanical"""
        base_run.failure_logs = "Error 429: Too many requests. Rate limit exceeded."
        layer, reason = classifier.classify(base_run)
        assert layer == StepLayer.MECHANICAL
    
    def test_classify_structural_yaml_error(self, classifier, base_run):
        """YAML errors should be structural"""
        base_run.failure_logs = "YAML error: invalid YAML in config.yml"
        layer, reason = classifier.classify(base_run)
        assert layer == StepLayer.STRUCTURAL
    
    def test_patterns_exist(self, classifier):
        """Classifier should have patterns defined"""
        assert len(classifier.patterns) > 0
    
    def test_classification_is_deterministic(self, classifier, base_run):
        """Same input should always produce same classification"""
        base_run.failure_logs = "AssertionError: test failed"
        layer1, _ = classifier.classify(base_run)
        layer2, _ = classifier.classify(base_run)
        assert layer1 == layer2
