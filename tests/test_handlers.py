"""Tests for Repair Handlers"""

import pytest
from src.handlers import MechanicalHandler, StructuralHandler, SemanticHandler
from src.models import Run, Step, StepType, StepLayer, StepStatus, Attribution


class TestMechanicalHandler:
    """Test Mechanical Handler"""
    
    @pytest.fixture
    def handler(self):
        """Create a fresh MechanicalHandler"""
        return MechanicalHandler(max_retries=3)
    
    @pytest.fixture
    def base_run(self):
        """Create a base Run for testing"""
        return Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="",
            diff="",
            commit_message="Test"
        )
    
    def test_handler_initialization(self, handler):
        """Test handler initializes correctly"""
        assert handler.max_retries == 3
        assert handler.initial_backoff_ms == 100
    
    def test_handle_timeout_error(self, handler, base_run):
        """Test handling timeout errors"""
        base_run.failure_logs = "Error: Connection timeout after 30 seconds"
        
        success, action = handler.handle(base_run)
        
        assert success is True
        assert "timeout" in action.lower()
        assert action.startswith("Retry")  # a real re-run, not an unapplied "increase timeout" claim
    
    def test_handle_rate_limit(self, handler, base_run):
        """Test handling rate limit errors"""
        base_run.failure_logs = "HTTP 429: Too Many Requests - rate limit exceeded"
        
        success, action = handler.handle(base_run)
        
        assert success is True
        assert "rate limit" in action.lower() and action.startswith("Retry")
    
    def test_handle_service_unavailable(self, handler, base_run):
        """Test handling service unavailable errors"""
        base_run.failure_logs = "503 Service Unavailable"
        
        success, action = handler.handle(base_run)
        
        assert success is True
        assert "retry" in action.lower()
    
    def test_handle_connection_error(self, handler, base_run):
        """Test handling connection errors"""
        base_run.failure_logs = "Error: Connection reset by peer"
        
        success, action = handler.handle(base_run)
        
        assert success is True
        assert "retry" in action.lower()
    
    def test_handle_unknown_error(self, handler, base_run):
        """Test handling unknown errors"""
        base_run.failure_logs = "Some mysterious error"
        
        success, action = handler.handle(base_run)
        
        assert success is False
        assert "no" in action.lower()
    
    def test_create_repair_step(self, handler, base_run):
        """Test creating a repair step"""
        step = handler.create_repair_step(base_run, True, "Retry with backoff")
        
        assert step.type == StepType.REPAIR
        assert step.layer == StepLayer.MECHANICAL
        assert step.status == StepStatus.SUCCESS
        assert step.output['success'] is True
    
    def test_add_repair_to_run(self, handler, base_run):
        """Test adding repair to run"""
        base_run.failure_logs = "Connection timeout"
        initial_steps = len(base_run.steps)
        
        handler.add_repair_to_run(base_run)
        
        assert len(base_run.steps) == initial_steps + 1
        assert base_run.steps[-1].type == StepType.REPAIR


class TestStructuralHandler:
    """Test Structural Handler"""
    
    @pytest.fixture
    def handler(self, structural_provider):
        """Create a StructuralHandler with configured provider"""
        return StructuralHandler(model_provider=structural_provider)
    
    @pytest.fixture
    def base_run(self):
        """Create a base Run for testing"""
        return Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="",
            diff="",
            commit_message="Test"
        )
    
    def test_handle_yaml_error(self, handler, base_run):
        """Test handling YAML errors"""
        base_run.failure_logs = "YAML parse error: mapping values are not allowed here"
        
        success, description, output = handler.handle(base_run)
        
        assert success is True
        assert "indentation" in description.lower() or "colon" in description.lower()
        assert output['issue'] == 'yaml_format'
    
    def test_handle_json_error(self, handler, base_run):
        """Test handling JSON errors"""
        base_run.failure_logs = "JSON decode error: Unexpected end of JSON input"
        
        success, description, output = handler.handle(base_run)
        
        assert success is True
        assert "incomplete" in description.lower() or "bracket" in description.lower()
        assert output['issue'] == 'json_format'
    
    def test_handle_lint_error(self, handler, base_run):
        """Test handling lint errors"""
        base_run.failure_logs = "ESLint error: Line 42 too long (102 characters)"
        
        success, description, output = handler.handle(base_run)
        
        assert success is True
        assert "lint" in description.lower() or "format" in description.lower()
        assert output['issue'] == 'lint_format'
    
    def test_handle_compilation_error(self, handler, base_run):
        """Test handling compilation errors"""
        base_run.failure_logs = "TypeScript compile error: Type 'string' is not assignable to type 'number'"
        
        success, description, output = handler.handle(base_run)
        
        assert success is True
        assert "type" in description.lower()
        assert output['issue'] == 'compilation'
    
    def test_handle_unknown_structural_error(self, handler, base_run):
        """Test handling unknown structural errors"""
        base_run.failure_logs = "Some build error"
        
        success, description, output = handler.handle(base_run)
        
        assert success is False
        assert "no" in description.lower()
    
    def test_create_repair_step(self, handler, base_run):
        """Test creating a repair step"""
        step = handler.create_repair_step(
            base_run,
            True,
            "Fix YAML indentation",
            {'issue': 'yaml_format'}
        )
        
        assert step.type == StepType.REPAIR
        assert step.layer == StepLayer.STRUCTURAL
        assert step.status == StepStatus.SUCCESS
        assert step.cost.tokens_used == 50  # SLM estimate
    
    def test_add_repair_to_run(self, handler, base_run):
        """Test adding repair to run"""
        base_run.failure_logs = "YAML error: mapping values not allowed"
        initial_steps = len(base_run.steps)
        
        handler.add_repair_to_run(base_run)
        
        assert len(base_run.steps) == initial_steps + 1
        assert base_run.steps[-1].type == StepType.REPAIR


class TestSemanticHandler:
    """Test Semantic Handler"""
    
    @pytest.fixture
    def handler(self, semantic_provider):
        """Create a SemanticHandler with configured provider"""
        return SemanticHandler(model_provider=semantic_provider)
    
    @pytest.fixture
    def base_run(self):
        """Create a base Run for testing"""
        run = Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="Test failed: expected 42 but got 41",
            diff="- return 42\n+ return 43",
            commit_message="Test"
        )
        
        # Add an attribution step
        attr = Attribution(
            claimed_cause="Changed return value from 42 to 43",
            evidence_for=["Value changed in diff"],
            counterfactual_result="pass"
        )
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            attribution=attr
        )
        run.add_step(step)
        
        return run
    
    def test_handle_with_attribution(self, handler, base_run):
        """Test handling semantic error with attribution"""
        success, description, output = handler.handle(base_run)
        
        assert success is True
        assert description is not None
        assert 'reasoning' in output
        assert 'suggested_approach' in output
    
    def test_handle_without_attribution(self, handler):
        """Test handling semantic error without attribution"""
        run = Run(repo="user/repo", failing_commit="abc123", failure_logs="Test failed", diff="")
        
        success, description, output = handler.handle(run)
        
        assert success is False
        assert "attribution" in description.lower()
    
    def test_suggest_approach_removed_code(self, handler):
        """Test suggesting approach for removed code"""
        attr = Attribution(claimed_cause="Removed return statement")
        
        approach = handler._suggest_approach(attr)
        
        assert "restore" in approach.lower()
    
    def test_suggest_approach_added_code(self, handler):
        """Test suggesting approach for added code"""
        attr = Attribution(claimed_cause="Added wrong operator")
        
        approach = handler._suggest_approach(attr)
        
        assert "fix" in approach.lower() or "remove" in approach.lower()
    
    def test_suggest_approach_null_error(self, handler):
        """Test suggesting approach for null reference"""
        attr = Attribution(claimed_cause="Missing null check")
        
        approach = handler._suggest_approach(attr)
        
        assert "null" in approach.lower() or "check" in approach.lower()
    
    def test_create_repair_step(self, handler, base_run):
        """Test creating a repair step"""
        step = handler.create_repair_step(
            base_run,
            True,
            "Fix logic error",
            {'suggested_approach': 'Revert change'}
        )
        
        assert step.type == StepType.REPAIR
        assert step.layer == StepLayer.SEMANTIC
        assert step.status == StepStatus.SUCCESS
        assert step.cost.tokens_used == 500  # LLM estimate
    
    def test_add_repair_to_run(self, handler, base_run):
        """Test adding repair to run"""
        initial_steps = len(base_run.steps)
        
        handler.add_repair_to_run(base_run)
        
        assert len(base_run.steps) == initial_steps + 1
        assert base_run.steps[-1].type == StepType.REPAIR
    
    def test_get_attribution_step(self, handler, base_run):
        """Test getting attribution step"""
        attr_step = handler._get_attribution_step(base_run)
        
        assert attr_step is not None
        assert attr_step.type == StepType.ATTRIBUTION
    
    def test_get_attribution_step_not_found(self, handler):
        """Test getting attribution when none exists"""
        run = Run(repo="user/repo", failing_commit="abc123", failure_logs="", diff="")
        
        attr_step = handler._get_attribution_step(run)
        
        assert attr_step is None
    
    def test_propose_fix_high_confidence(self, handler, base_run):
        """Test proposing high-confidence fix"""
        attr_step = handler._get_attribution_step(base_run)
        fix = handler._propose_fix(base_run, attr_step)
        
        assert fix['confidence'] == 'high'
        assert fix['files_affected'] == 1
        assert fix['estimated_lines_changed'] == 5
    
    def test_cost_estimate_semantic_vs_mechanical(self, handler):
        """Test that semantic repairs have higher cost than mechanical"""
        run = Run(repo="user/repo", failing_commit="abc123", failure_logs="", diff="")
        
        # Add attribution for semantic
        attr = Attribution(claimed_cause="Logic error")
        step = Step(type=StepType.ATTRIBUTION, attribution=attr)
        run.add_step(step)
        
        semantic_step = handler.create_repair_step(run, True, "Fix", {})
        
        # Compare to mechanical (created in test_handlers.py)
        mech_handler = MechanicalHandler()
        mech_step = mech_handler.create_repair_step(run, True, "Retry")
        
        assert semantic_step.cost.tokens_used > mech_step.cost.tokens_used
