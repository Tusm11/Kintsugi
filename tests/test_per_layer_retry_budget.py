"""Test per-layer retry budget isolation"""

import os
import pytest
from src.models import Run, Budget, Cost
from src.budget import BudgetGuard
from src.handlers import MechanicalHandler, StructuralHandler, SemanticHandler
from src.model_provider import GroqProvider, MockProvider


class TestPerLayerRetryBudget:
    """Test that retry budgets are isolated per layer"""
    
    @pytest.fixture
    def budget_guard(self):
        """Create a budget guard"""
        return BudgetGuard()
    
    @pytest.fixture
    def run_with_per_layer_budget(self):
        """Create a run with per-layer retry budgets"""
        budget = Budget(
            max_tokens=10000,
            max_retries_mechanical=3,
            max_retries_structural=2,
            max_retries_semantic=1,
            max_wall_clock_ms=300000
        )
        return Run(
            repo="test/repo",
            failing_commit="abc123",
            budget=budget,
            failure_logs="Test failure"
        )
    
    def test_mechanical_retry_exhaustion_does_not_affect_semantic(self, budget_guard, run_with_per_layer_budget):
        """Test that exhausting mechanical retries doesn't affect semantic retries"""
        run = run_with_per_layer_budget
        budget_guard.initialize_budget(run)
        
        # Exhaust mechanical retries
        for i in range(3):
            success, msg = budget_guard.record_retry(run.id, "mechanical")
            assert success is True, f"Mechanical retry {i+1} should succeed"
        
        # Fourth mechanical retry should fail
        success, msg = budget_guard.record_retry(run.id, "mechanical")
        assert success is False
        assert "Mechanical retry budget exceeded" in msg
        
        # But semantic retries should still work
        success, msg = budget_guard.record_retry(run.id, "semantic")
        assert success is True, "Semantic retry should succeed despite mechanical exhaustion"
    
    def test_semantic_retry_exhaustion_does_not_affect_structural(self, budget_guard, run_with_per_layer_budget):
        """Test that exhausting semantic retries doesn't affect structural retries"""
        run = run_with_per_layer_budget
        budget_guard.initialize_budget(run)
        
        # Exhaust semantic retries
        success, msg = budget_guard.record_retry(run.id, "semantic")
        assert success is True
        
        # Second semantic retry should fail
        success, msg = budget_guard.record_retry(run.id, "semantic")
        assert success is False
        assert "Semantic retry budget exceeded" in msg
        
        # But structural retries should still work
        success, msg = budget_guard.record_retry(run.id, "structural")
        assert success is True, "Structural retry should succeed despite semantic exhaustion"
    
    def test_each_layer_increments_independently(self, budget_guard, run_with_per_layer_budget):
        """Test that retry counts for each layer are independent"""
        run = run_with_per_layer_budget
        budget_guard.initialize_budget(run)
        
        # Record one retry for each layer
        for layer in ["mechanical", "structural", "semantic"]:
            success, msg = budget_guard.record_retry(run.id, layer)
            assert success is True
        
        # Check status
        status = budget_guard.get_budget_status(run.id)
        assert status['retries_mechanical']['used'] == 1
        assert status['retries_structural']['used'] == 1
        assert status['retries_semantic']['used'] == 1
        
        # Add more to mechanical
        for _ in range(2):
            success, msg = budget_guard.record_retry(run.id, "mechanical")
            assert success is True
        
        # Check status again
        status = budget_guard.get_budget_status(run.id)
        assert status['retries_mechanical']['used'] == 3
        assert status['retries_structural']['used'] == 1  # Unchanged
        assert status['retries_semantic']['used'] == 1    # Unchanged
    
    def test_per_layer_budgets_read_from_env(self, budget_guard, run_with_per_layer_budget):
        """Test that per-layer budgets are read from environment variables"""
        # Set custom budgets
        os.environ["MAX_RETRIES_MECHANICAL"] = "7"
        os.environ["MAX_RETRIES_STRUCTURAL"] = "4"
        os.environ["MAX_RETRIES_SEMANTIC"] = "2"
        
        try:
            run = run_with_per_layer_budget
            budget_guard.initialize_budget(run)
            
            status = budget_guard.get_budget_status(run.id)
            assert status['retries_mechanical']['max'] == 7
            assert status['retries_structural']['max'] == 4
            assert status['retries_semantic']['max'] == 2
        finally:
            # Clean up
            for var in ["MAX_RETRIES_MECHANICAL", "MAX_RETRIES_STRUCTURAL", "MAX_RETRIES_SEMANTIC"]:
                os.environ.pop(var, None)
    
    def test_invalid_layer_name_rejected(self, budget_guard, run_with_per_layer_budget):
        """Test that invalid layer names are rejected"""
        run = run_with_per_layer_budget
        budget_guard.initialize_budget(run)
        
        success, msg = budget_guard.record_retry(run.id, "invalid_layer")
        assert success is False
        assert "Invalid layer" in msg
    
    def test_exhaustion_check_per_layer(self, budget_guard, run_with_per_layer_budget):
        """Test that exhaustion check respects per-layer limits"""
        run = run_with_per_layer_budget
        budget_guard.initialize_budget(run)
        
        # Exhaust semantic (only 1 allowed)
        budget_guard.record_retry(run.id, "semantic")
        
        # Check if budget is exhausted
        exhausted, msg = budget_guard.is_budget_exhausted(run.id)
        assert exhausted is True
        assert "Semantic retry budget exhausted" in msg
    
    def test_mechanical_handlers_report_per_layer_retry(self):
        """Test that mechanical handler correctly reports retries to its layer"""
        budget_guard = BudgetGuard()
        handler = MechanicalHandler(max_retries=2)
        
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            budget=Budget(
                max_retries_mechanical=5,
                max_retries_structural=3,
                max_retries_semantic=2
            ),
            failure_logs="Connection timeout"
        )
        
        budget_guard.initialize_budget(run)
        
        # First repair (with mechanical retry)
        success = handler.add_repair_to_run(run, budget_guard=budget_guard)
        
        status = budget_guard.get_budget_status(run.id)
        assert status['retries_mechanical']['used'] == 1
        assert status['retries_structural']['used'] == 0
        assert status['retries_semantic']['used'] == 0
    
    def test_structural_handlers_report_per_layer_retry(self):
        """Test that structural handler correctly reports retries to its layer"""
        budget_guard = BudgetGuard()
        handler = StructuralHandler(model_provider=MockProvider("Mock structural"))
        
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            budget=Budget(
                max_retries_mechanical=5,
                max_retries_structural=3,
                max_retries_semantic=2
            ),
            failure_logs="YAML error"
        )
        
        budget_guard.initialize_budget(run)
        
        # First repair (with structural retry)
        success = handler.add_repair_to_run(run, budget_guard=budget_guard)
        
        status = budget_guard.get_budget_status(run.id)
        assert status['retries_mechanical']['used'] == 0
        assert status['retries_structural']['used'] == 1
        assert status['retries_semantic']['used'] == 0
    
    def test_semantic_handlers_report_per_layer_retry(self):
        """Test that semantic handler correctly reports retries to its layer"""
        budget_guard = BudgetGuard()
        handler = SemanticHandler(model_provider=MockProvider("Mock semantic"))
        
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            budget=Budget(
                max_retries_mechanical=5,
                max_retries_structural=3,
                max_retries_semantic=2
            ),
            failure_logs="Test failure"
        )
        
        budget_guard.initialize_budget(run)
        
        # First repair (with semantic retry)
        success = handler.add_repair_to_run(run, budget_guard=budget_guard)
        
        status = budget_guard.get_budget_status(run.id)
        assert status['retries_mechanical']['used'] == 0
        assert status['retries_structural']['used'] == 0
        assert status['retries_semantic']['used'] == 1
    
    def test_run_spent_tracks_per_layer_retries(self, budget_guard, run_with_per_layer_budget):
        """Test that Run.spent tracks per-layer retry counts"""
        run = run_with_per_layer_budget
        budget_guard.initialize_budget(run)
        
        # Record retries for each layer
        budget_guard.record_retry(run.id, "mechanical")
        budget_guard.record_retry(run.id, "mechanical")
        budget_guard.record_retry(run.id, "structural")
        budget_guard.record_retry(run.id, "semantic")
        
        # Sync spent budget to run
        budget_guard.sync_run_budget(run)
        
        assert run.spent.retries_used_mechanical == 2
        assert run.spent.retries_used_structural == 1
        assert run.spent.retries_used_semantic == 1
    
    def test_overall_retry_ceiling_enforced(self, budget_guard):
        """Test that overall retry ceiling is enforced as secondary safety net"""
        # Create a run with tight overall limit
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            budget=Budget(
                max_retries_mechanical=5,
                max_retries_structural=3,
                max_retries_semantic=2,
                max_retries_total=3  # Only 3 total allowed despite 10 per-layer sum
            ),
            failure_logs="Test failure"
        )
        
        budget_guard.initialize_budget(run)
        
        # Record retries across layers up to overall ceiling
        success, msg = budget_guard.record_retry(run.id, "mechanical")
        assert success is True
        assert "total: 1/3" in msg
        
        success, msg = budget_guard.record_retry(run.id, "structural")
        assert success is True
        assert "total: 2/3" in msg
        
        success, msg = budget_guard.record_retry(run.id, "semantic")
        assert success is True
        assert "total: 3/3" in msg
        
        # Fourth retry should fail due to overall ceiling, even though each layer has budget
        success, msg = budget_guard.record_retry(run.id, "mechanical")
        assert success is False
        assert "Overall retry budget exceeded" in msg
        assert "4 total retries > 3 max allowed" in msg
    
    def test_exhaustion_message_distinguishes_total_vs_layer(self, budget_guard):
        """Test that exhaustion messages clearly distinguish total vs per-layer limits"""
        run = Run(
            repo="test/repo",
            failing_commit="abc123",
            budget=Budget(
                max_retries_mechanical=2,
                max_retries_structural=3,
                max_retries_semantic=2,
                max_retries_total=5
            ),
            failure_logs="Test failure"
        )
        
        budget_guard.initialize_budget(run)
        
        # Exhaust total first
        for _ in range(5):
            budget_guard.record_retry(run.id, "mechanical")
        
        # Check exhaustion — should report total, not mechanical
        exhausted, msg = budget_guard.is_budget_exhausted(run.id)
        assert exhausted is True
        assert "Overall retry budget exhausted" in msg
        assert "5/5" in msg
        
        # Now test when layer is exhausted first
        run2 = Run(
            repo="test/repo",
            failing_commit="abc123",
            budget=Budget(
                max_retries_mechanical=2,
                max_retries_structural=10,
                max_retries_semantic=10,
                max_retries_total=20
            ),
            failure_logs="Test failure"
        )
        
        budget_guard.initialize_budget(run2)
        
        # Exhaust mechanical layer
        for _ in range(2):
            budget_guard.record_retry(run2.id, "mechanical")
        
        # Check exhaustion — should report mechanical, not total
        exhausted, msg = budget_guard.is_budget_exhausted(run2.id)
        assert exhausted is True
        assert "Mechanical retry budget exhausted" in msg
        assert "2/2" in msg
    
    def test_default_per_layer_budgets_are_reasonable(self):
        """Test that default per-layer budgets make sense"""
        budget = Budget()
        
        # Mechanical should allow most retries (cheap, no LLM)
        assert budget.max_retries_mechanical >= 3
        
        # Structural should allow moderate retries (SLM cost)
        assert budget.max_retries_structural >= 2
        
        # Semantic should allow fewest retries (LLM expensive)
        assert budget.max_retries_semantic >= 1
        
        # Mechanical should be >= structural >= semantic
        assert budget.max_retries_mechanical >= budget.max_retries_structural
        assert budget.max_retries_structural >= budget.max_retries_semantic
        
        # Total should be at least the sum of layer maximums
        assert budget.max_retries_total >= (budget.max_retries_mechanical + budget.max_retries_structural + budget.max_retries_semantic)
