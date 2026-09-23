"""Tests for Repair Router"""

import pytest
from src.repair_router import RepairRouter, HandlerType, ModelTier
from src.models import Run, Step, StepType, StepLayer, StepStatus


class TestRepairRouter:
    """Test Repair Router"""
    
    @pytest.fixture
    def router(self):
        """Create a fresh RepairRouter"""
        return RepairRouter()
    
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
    
    def test_routing_table_exists(self, router):
        """Test that routing table is properly initialized"""
        assert StepLayer.MECHANICAL in router.ROUTING_TABLE
        assert StepLayer.STRUCTURAL in router.ROUTING_TABLE
        assert StepLayer.SEMANTIC in router.ROUTING_TABLE
    
    def test_routing_mechanical_no_model(self, router, base_run):
        """Test mechanical failures route to no model"""
        # Add classification step
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.MECHANICAL,
            output={'classified_layer': 'mechanical'}
        )
        base_run.add_step(step)
        
        handler, model_tier, reason = router.route(base_run)
        
        assert handler == HandlerType.MECHANICAL
        assert model_tier == ModelTier.NONE
    
    def test_routing_structural_slm_model(self, router, base_run):
        """Test structural failures route to SLM"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.STRUCTURAL,
            output={'classified_layer': 'structural'}
        )
        base_run.add_step(step)
        
        handler, model_tier, reason = router.route(base_run)
        
        assert handler == HandlerType.STRUCTURAL
        assert model_tier == ModelTier.SLM
    
    def test_routing_semantic_llm_model(self, router, base_run):
        """Test semantic failures route to LLM"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            output={'classified_layer': 'semantic'}
        )
        base_run.add_step(step)
        
        handler, model_tier, reason = router.route(base_run)
        
        assert handler == HandlerType.SEMANTIC
        assert model_tier == ModelTier.LLM
    
    def test_routing_defaults_to_semantic_if_unclassified(self, router, base_run):
        """Test that unclassified runs default to semantic"""
        # Don't add classification step
        
        handler, model_tier, reason = router.route(base_run)
        
        # Should default to semantic
        assert handler == HandlerType.SEMANTIC
        assert model_tier == ModelTier.LLM
    
    def test_capacity_upgrade_structural_large_diff(self, router, base_run):
        """Test that large structural patches are upgraded to LLM"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.STRUCTURAL,
            output={'classified_layer': 'structural'}
        )
        base_run.add_step(step)
        
        # Create a large diff (>100 lines)
        base_run.diff = "\n".join([f"+ line {i}" for i in range(101)])
        
        handler, model_tier, reason = router.route(base_run)
        
        # Should upgrade to LLM for capacity reasons
        assert model_tier == ModelTier.LLM
    
    def test_capacity_upgrade_structural_many_files(self, router, base_run):
        """Test that multi-file structural patches are upgraded to LLM"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.STRUCTURAL,
            output={'classified_layer': 'structural'}
        )
        base_run.add_step(step)
        
        # Create a diff touching 4 files
        base_run.diff = "\n".join([
            f"+++ b/file{i}.py\n+ change {i}"
            for i in range(4)
        ])
        
        handler, model_tier, reason = router.route(base_run)
        
        # Should upgrade to LLM for capacity reasons
        assert model_tier == ModelTier.LLM
    
    def test_no_capacity_upgrade_small_structural(self, router, base_run):
        """Test that small structural patches stay at SLM"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.STRUCTURAL,
            output={'classified_layer': 'structural'}
        )
        base_run.add_step(step)
        
        # Small diff
        base_run.diff = "+ one change\n+ another change"
        
        handler, model_tier, reason = router.route(base_run)
        
        # Should stay at SLM
        assert model_tier == ModelTier.SLM
    
    def test_create_routing_step(self, router, base_run):
        """Test creating a routing step"""
        step = router.create_routing_step(
            base_run,
            HandlerType.SEMANTIC,
            ModelTier.LLM,
            "Semantic failure requires LLM reasoning"
        )
        
        assert step.type == StepType.VERIFICATION
        assert step.output['selected_handler'] == 'semantic'
        assert step.output['selected_model_tier'] == 'llm'
    
    def test_add_routing_to_run(self, router, base_run):
        """Test adding routing step to run"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            output={'classified_layer': 'semantic'}
        )
        base_run.add_step(step)
        
        initial_steps = len(base_run.steps)
        
        handler, model_tier = router.add_routing_to_run(base_run)
        
        assert len(base_run.steps) == initial_steps + 1
        last_step = base_run.steps[-1]
        assert last_step.type == StepType.VERIFICATION
        assert last_step.output['selected_handler'] == 'semantic'
    
    def test_routing_decisions_audit_trail(self, router, base_run):
        """Test that routing decisions are recorded for audit"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.MECHANICAL,
            output={'classified_layer': 'mechanical'}
        )
        base_run.add_step(step)
        
        router.route(base_run)
        
        decisions = router.get_routing_decisions()
        assert len(decisions) == 1
        assert decisions[0]['run_id'] == base_run.id
        assert decisions[0]['layer'] == 'mechanical'
    
    def test_multiple_routing_decisions_tracked(self, router):
        """Test that multiple routing decisions are tracked"""
        for i in range(3):
            run = Run(repo=f"repo{i}", failing_commit=f"commit{i}")
            step = Step(
                type=StepType.ATTRIBUTION,
                output={'classified_layer': 'semantic'}
            )
            run.add_step(step)
            router.route(run)
        
        decisions = router.get_routing_decisions()
        assert len(decisions) == 3
    
    def test_get_classified_layer_from_steps(self, router, base_run):
        """Test extracting classified layer from steps"""
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.STRUCTURAL,
            output={'classified_layer': 'structural'}
        )
        base_run.add_step(step)
        
        layer = router._get_classified_layer(base_run)
        
        assert layer == StepLayer.STRUCTURAL
    
    def test_get_classified_layer_not_found(self, router, base_run):
        """Test that None is returned when no classification found"""
        # Don't add classification step
        
        layer = router._get_classified_layer(base_run)
        
        assert layer is None
    
    def test_get_classified_layer_uses_most_recent(self, router, base_run):
        """Test that most recent classification is used"""
        step1 = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.MECHANICAL,
            output={'classified_layer': 'mechanical'}
        )
        step2 = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            output={'classified_layer': 'semantic'}
        )
        base_run.add_step(step1)
        base_run.add_step(step2)
        
        layer = router._get_classified_layer(base_run)
        
        # Should use the most recent (step2)
        assert layer == StepLayer.SEMANTIC
    
    def test_routing_deterministic(self, router):
        """Test that routing is deterministic for same input"""
        run1 = Run(repo="repo", failing_commit="commit1")
        run2 = Run(repo="repo", failing_commit="commit1")
        
        step1 = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.STRUCTURAL,
            output={'classified_layer': 'structural'}
        )
        step2 = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.STRUCTURAL,
            output={'classified_layer': 'structural'}
        )
        
        run1.add_step(step1)
        run2.add_step(step2)
        
        handler1, tier1, _ = router.route(run1)
        handler2, tier2, _ = router.route(run2)
        
        assert handler1 == handler2
        assert tier1 == tier2
    
    def test_routing_table_has_all_required_fields(self, router):
        """Test that routing table entries have required fields"""
        for layer, routing in router.ROUTING_TABLE.items():
            assert 'handler' in routing
            assert 'model_tier' in routing
            assert 'description' in routing
            assert routing['handler'] in HandlerType.__members__.values()
            assert routing['model_tier'] in ModelTier.__members__.values()
