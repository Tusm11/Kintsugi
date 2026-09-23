"""Tests for Data Models"""

import pytest
from datetime import datetime
from src.models import (
    Step, Run, Attribution, Cost, Budget,
    StepType, StepLayer, StepStatus, RunStatus
)


class TestCost:
    """Test Cost model with per-layer retry tracking"""
    
    def test_cost_initialization(self):
        cost = Cost(tokens_used=100, wall_clock_ms=1000)
        assert cost.tokens_used == 100
        assert cost.wall_clock_ms == 1000
    
    def test_cost_default_values(self):
        cost = Cost()
        assert cost.tokens_used == 0
        assert cost.wall_clock_ms == 0
        assert cost.retries_used_mechanical == 0
        assert cost.retries_used_structural == 0
        assert cost.retries_used_semantic == 0
    
    def test_cost_with_per_layer_retries(self):
        """Cost should track retries per layer"""
        cost = Cost(
            tokens_used=500,
            wall_clock_ms=5000,
            retries_used_mechanical=2,
            retries_used_structural=1,
            retries_used_semantic=0
        )
        assert cost.retries_used_mechanical == 2
        assert cost.retries_used_structural == 1
        assert cost.retries_used_semantic == 0
    
    def test_cost_to_dict(self):
        """Cost.to_dict() should include all fields"""
        cost = Cost(
            tokens_used=100,
            wall_clock_ms=1000,
            retries_used_mechanical=1,
            retries_used_structural=0,
            retries_used_semantic=1
        )
        result = cost.to_dict()
        assert result['tokens_used'] == 100
        assert result['wall_clock_ms'] == 1000
        assert result['retries_used_mechanical'] == 1
        assert result['retries_used_structural'] == 0
        assert result['retries_used_semantic'] == 1
    
    def test_cost_from_dict(self):
        data = {
            'tokens_used': 100,
            'wall_clock_ms': 1000,
            'retries_used_mechanical': 1,
            'retries_used_structural': 0,
            'retries_used_semantic': 1
        }
        cost = Cost.from_dict(data)
        assert cost.tokens_used == 100
        assert cost.wall_clock_ms == 1000
        assert cost.retries_used_mechanical == 1
        assert cost.retries_used_semantic == 1


class TestBudget:
    """Test Budget model with per-layer retry limits"""
    
    def test_budget_initialization(self):
        budget = Budget(
            max_tokens=5000,
            max_retries_mechanical=5,
            max_retries_structural=3,
            max_retries_semantic=2,
            max_wall_clock_ms=150000
        )
        assert budget.max_tokens == 5000
        assert budget.max_retries_mechanical == 5
        assert budget.max_retries_structural == 3
        assert budget.max_retries_semantic == 2
        assert budget.max_wall_clock_ms == 150000
    
    def test_budget_defaults(self):
        """Budget should have reasonable defaults"""
        budget = Budget()
        assert budget.max_tokens == 10000
        assert budget.max_retries_mechanical == 5
        assert budget.max_retries_structural == 3
        assert budget.max_retries_semantic == 2
        assert budget.max_wall_clock_ms == 300000
        assert budget.max_retries_total == 7  # Overall ceiling
    
    def test_budget_to_dict(self):
        """Budget.to_dict() should include all fields"""
        budget = Budget(
            max_tokens=5000,
            max_retries_mechanical=5,
            max_retries_structural=3,
            max_retries_semantic=2,
            max_wall_clock_ms=150000,
            max_retries_total=7
        )
        result = budget.to_dict()
        assert result['max_tokens'] == 5000
        assert result['max_retries_mechanical'] == 5
        assert result['max_retries_structural'] == 3
        assert result['max_retries_semantic'] == 2
        assert result['max_wall_clock_ms'] == 150000
        assert result['max_retries_total'] == 7
    
    def test_budget_from_dict(self):
        """Budget.from_dict() should reconstruct correctly"""
        data = {
            'max_tokens': 5000,
            'max_retries_mechanical': 5,
            'max_retries_structural': 3,
            'max_retries_semantic': 2,
            'max_wall_clock_ms': 150000,
            'max_retries_total': 7
        }
        budget = Budget.from_dict(data)
        assert budget.max_tokens == 5000
        assert budget.max_retries_total == 7


class TestAttribution:
    """Test Attribution model with source tracking"""
    
    def test_attribution_initialization(self):
        attr = Attribution(
            claimed_cause="Memory leak in function X",
            evidence_for=["Heap growing unbounded", "GC not collecting"],
            evidence_against=["No allocation increase"],
            counterfactual_result="pass"
        )
        assert attr.claimed_cause == "Memory leak in function X"
        assert len(attr.evidence_for) == 2
        assert attr.counterfactual_result == "pass"
    
    def test_attribution_defaults(self):
        """Attribution should have sensible defaults"""
        attr = Attribution(claimed_cause="Test failed")
        assert attr.claimed_cause == "Test failed"
        assert attr.evidence_for == []
        assert attr.evidence_against == []
        assert attr.alternatives_considered == []
        assert attr.counterfactual_result is None
        assert attr.attribution_source == "model"  # Default: assume model
    
    def test_attribution_no_strength_fields(self):
        """Attribution should NOT have strength-related fields (no false precision)"""
        attr = Attribution(claimed_cause="Test failed")
        assert not hasattr(attr, 'evidence_strength')
        assert not hasattr(attr, 'evidence_strength_source')
    
    def test_attribution_to_dict(self):
        attr = Attribution(
            claimed_cause="Bug in parser",
            evidence_for=["Exception thrown"],
            evidence_against=[],
            counterfactual_result="pass",
            attribution_source="model"
        )
        result = attr.to_dict()
        assert result['claimed_cause'] == "Bug in parser"
        assert result['counterfactual_result'] == "pass"
        assert result['attribution_source'] == "model"
    
    def test_attribution_from_dict(self):
        data = {
            'claimed_cause': "Logic error",
            'evidence_for': ["X is true", "Y is false"],
            'evidence_against': [],
            'alternatives_considered': [],
            'counterfactual_result': "pass",
            'attribution_source': "model"
        }
        attr = Attribution.from_dict(data)
        assert attr.claimed_cause == "Logic error"
        assert len(attr.evidence_for) == 2
        assert attr.attribution_source == "model"
    
    def test_attribution_from_dict_with_legacy_field(self):
        """from_dict should handle old serialized data with evidence_strength_source"""
        data = {
            'claimed_cause': "Logic error",
            'evidence_for': ["X is true"],
            'evidence_against': [],
            'alternatives_considered': [],
            'counterfactual_result': "pass",
            'attribution_source': "model",
            'evidence_strength_source': "n/a"  # Legacy field, should be silently dropped
        }
        attr = Attribution.from_dict(data)
        assert attr.claimed_cause == "Logic error"
        assert attr.attribution_source == "model"
        assert not hasattr(attr, 'evidence_strength_source')


class TestStep:
    """Test Step model"""
    
    def test_step_initialization(self):
        step = Step(type=StepType.ATTRIBUTION, layer=StepLayer.SEMANTIC)
        assert step.type == StepType.ATTRIBUTION
        assert step.layer == StepLayer.SEMANTIC
        assert step.status == StepStatus.PENDING
    
    def test_step_defaults(self):
        step = Step()
        assert step.type == StepType.ATTRIBUTION
        assert step.status == StepStatus.PENDING
        assert step.attempts == 0
        assert step.attribution is None
    
    def test_step_unique_ids(self):
        """Each step should have unique ID"""
        step1 = Step()
        step2 = Step()
        assert step1.id != step2.id
    
    def test_step_with_attribution(self):
        attr = Attribution(claimed_cause="Test cause")
        step = Step(attribution=attr)
        assert step.attribution == attr
        assert step.attribution.claimed_cause == "Test cause"
    
    def test_step_to_dict(self):
        """Step.to_dict() should serialize correctly"""
        attr = Attribution(claimed_cause="Bug")
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            attribution=attr
        )
        result = step.to_dict()
        assert result['type'] == 'attribution'
        assert result['layer'] == 'semantic'
        assert result['status'] == 'success'
        assert result['attribution']['claimed_cause'] == "Bug"
    
    def test_step_from_dict(self):
        data = {
            'id': 'test-id',
            'type': 'attribution',
            'layer': 'semantic',
            'status': 'success',
            'input': {},
            'output': {},
            'attempts': 0,
            'cost': {'tokens_used': 0, 'wall_clock_ms': 0, 'retries_used_mechanical': 0, 'retries_used_structural': 0, 'retries_used_semantic': 0},
            'attribution': {'claimed_cause': 'Bug', 'evidence_for': [], 'evidence_against': [], 'alternatives_considered': [], 'counterfactual_result': None, 'attribution_source': 'model'},
            'error_message': None,
            'timestamp_created': datetime.utcnow().isoformat(),
            'timestamp_started': None,
            'timestamp_completed': None
        }
        step = Step.from_dict(data)
        assert step.type == StepType.ATTRIBUTION
        assert step.layer == StepLayer.SEMANTIC


class TestRun:
    """Test Run model"""
    
    def test_run_initialization(self):
        run = Run(repo="user/repo", failing_commit="abc123")
        assert run.repo == "user/repo"
        assert run.failing_commit == "abc123"
        assert run.final_status == RunStatus.IN_PROGRESS
    
    def test_run_defaults(self):
        """Run should have sensible defaults"""
        run = Run()
        assert run.steps == []
        assert run.final_status == RunStatus.IN_PROGRESS
        assert run.spent.tokens_used == 0
    
    def test_run_add_step(self):
        """Run should track steps"""
        run = Run()
        step = Step(type=StepType.ATTRIBUTION)
        run.add_step(step)
        assert len(run.steps) == 1
        assert run.steps[0] == step
    
    def test_run_add_multiple_steps(self):
        run = Run()
        step1 = Step(type=StepType.ATTRIBUTION)
        step2 = Step(type=StepType.REPAIR)
        run.add_step(step1)
        run.add_step(step2)
        assert len(run.steps) == 2
    
    def test_run_get_last_step(self):
        run = Run()
        step1 = Step(type=StepType.ATTRIBUTION)
        step2 = Step(type=StepType.REPAIR)
        run.add_step(step1)
        run.add_step(step2)
        assert run.get_last_step() == step2
    
    def test_run_is_budget_exceeded_tokens(self):
        """Run should detect token budget exceeded"""
        run = Run(budget=Budget(max_tokens=1000))
        run.spent.tokens_used = 1001
        assert run.is_budget_exceeded()
    
    def test_run_is_budget_exceeded_time(self):
        """Run should detect time budget exceeded"""
        run = Run(budget=Budget(max_wall_clock_ms=5000))
        run.spent.wall_clock_ms = 5001
        assert run.is_budget_exceeded()
    
    def test_run_to_dict(self):
        """Run.to_dict() should serialize"""
        run = Run(repo="test/repo", failing_commit="abc")
        result = run.to_dict()
        assert result['repo'] == "test/repo"
        assert result['failing_commit'] == "abc"
        assert result['final_status'] == 'in_progress'
    
    def test_run_from_dict(self):
        """Run.from_dict() should deserialize"""
        data = {
            'id': 'test-id',
            'repo': 'test/repo',
            'failing_commit': 'abc123',
            'steps': [],
            'budget': {'max_tokens': 10000, 'max_retries_mechanical': 5, 'max_retries_structural': 3, 'max_retries_semantic': 2, 'max_retries_total': 7, 'max_wall_clock_ms': 300000},
            'spent': {'tokens_used': 0, 'wall_clock_ms': 0, 'retries_used_mechanical': 0, 'retries_used_structural': 0, 'retries_used_semantic': 0},
            'final_status': 'in_progress',
            'failure_logs': '',
            'diff': '',
            'commit_message': '',
            'timestamp_created': datetime.utcnow().isoformat(),
            'timestamp_completed': None,
            'metadata': {}
        }
        run = Run.from_dict(data)
        assert run.repo == 'test/repo'
        assert run.failing_commit == 'abc123'
    
    def test_run_with_steps_serialization(self):
        """Run with steps should serialize/deserialize"""
        run = Run(repo="test/repo")
        step = Step(type=StepType.ATTRIBUTION)
        attr = Attribution(claimed_cause="Bug")
        step.attribution = attr
        run.add_step(step)
        
        # Serialize
        data = run.to_dict()
        assert len(data['steps']) == 1
        
        # Deserialize
        run2 = Run.from_dict(data)
        assert len(run2.steps) == 1
        assert run2.steps[0].attribution.claimed_cause == "Bug"
