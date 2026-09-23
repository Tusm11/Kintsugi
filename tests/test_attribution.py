"""Tests for Attribution Engine"""

import pytest
from src.attribution import AttributionEngine, CounterfactualResult
from src.models import Run, StepLayer, StepType, StepStatus


class TestCounterfactualResult:
    """Test CounterfactualResult dataclass"""
    
    def test_counterfactual_result_creation(self):
        """CounterfactualResult should contain outcome and details, NOT evidence_strength"""
        result = CounterfactualResult(
            cause="Removed return statement",
            original_outcome="fail",
            counterfactual_outcome="pass",
            details="Test would pass if return was restored"
        )
        assert result.cause == "Removed return statement"
        assert result.counterfactual_outcome == "pass"
        assert result.original_outcome == "fail"
        assert result.details == "Test would pass if return was restored"
        # Verify evidence_strength field is gone (no false precision)
        assert not hasattr(result, 'evidence_strength')
    
    def test_counterfactual_result_inconclusive(self):
        """CounterfactualResult should support inconclusive outcome"""
        result = CounterfactualResult(
            cause="Complex logic change",
            original_outcome="fail",
            counterfactual_outcome="inconclusive",
            details="Cannot determine outcome with certainty"
        )
        assert result.counterfactual_outcome == "inconclusive"
    
    def test_counterfactual_result_fail(self):
        """CounterfactualResult should support fail outcome"""
        result = CounterfactualResult(
            cause="Added buggy code",
            original_outcome="fail",
            counterfactual_outcome="fail",
            details="Even with fix, test would still fail"
        )
        assert result.counterfactual_outcome == "fail"


class TestAttributionEngine:
    """Test Attribution Engine"""
    
    @pytest.fixture
    def engine(self):
        """Create a fresh AttributionEngine"""
        return AttributionEngine()
    
    @pytest.fixture
    def base_run(self):
        """Create a base Run for testing"""
        return Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="Test failed",
            diff="",
            commit_message="Fix bug"
        )
    
    def test_engine_initialization(self, engine):
        """Test attribution engine initializes correctly"""
        assert len(engine.counterfactual_cache) == 0
    
    def test_parse_diff_changes_additions(self, engine):
        """Test parsing added lines from diff"""
        diff = """--- a/file.py
+++ b/file.py
@@ -5,5 +5,5 @@
 unchanged line
+added line
 more unchanged
"""
        changes = engine._parse_diff_changes(diff)
        assert any(c['change_type'] == 'added' for c in changes)
        assert any('added line' in c['line'] for c in changes)
    
    def test_parse_diff_changes_removals(self, engine):
        """Test parsing removed lines from diff"""
        diff = """--- a/file.py
+++ b/file.py
@@ -5,5 +5,4 @@
 unchanged line
-removed line
 more unchanged
"""
        changes = engine._parse_diff_changes(diff)
        assert any(c['change_type'] == 'removed' for c in changes)
    
    def test_parse_diff_changes_complex(self, engine):
        """Test parsing complex diffs with multiple change types"""
        diff = """--- a/file.py
+++ b/file.py
@@ -1,10 +1,11 @@
 def func():
-    old_code()
+    new_code()
     x = 1
+    added_line()
     return x
"""
        changes = engine._parse_diff_changes(diff)
        assert len(changes) >= 3
        assert any(c['change_type'] == 'removed' for c in changes)
        assert any(c['change_type'] == 'added' for c in changes)
    
    def test_engine_initialization_creates_cache(self, engine):
        """Engine should have counterfactual cache"""
        assert hasattr(engine, 'counterfactual_cache')
        assert isinstance(engine.counterfactual_cache, dict)
