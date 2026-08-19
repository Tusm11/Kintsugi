"""Repair Router: Deterministically selects handler and model tier for repairs"""

from typing import Dict, Any, Tuple, Optional
from enum import Enum
from src.models import Run, StepLayer, Step, StepType, StepStatus


class ModelTier(str, Enum):
    """Available model tiers for repairs"""
    NONE = "none"  # No model required
    SLM = "slm"    # Small language model
    LLM = "llm"    # Large language model


class HandlerType(str, Enum):
    """Types of repair handlers"""
    MECHANICAL = "mechanical"
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"


class RepairRouter:
    """
    Deterministic repair routing based on failure classification.
    
    Never uses an LLM to decide whether to use an LLM. Routing is a lookup,
    not a reasoning task. Each failure layer has a fixed handler and model tier.
    
    Routing decisions are based on:
    1. Failure layer (mechanical, structural, semantic)
    2. Patch size (complexity of change needed)
    3. Scope (how many files affected)
    
    All routing is deterministic — no randomness, no per-request variation.
    """
    
    # Default routing table
    ROUTING_TABLE: Dict[StepLayer, Dict[str, Any]] = {
        StepLayer.MECHANICAL: {
            'handler': HandlerType.MECHANICAL,
            'model_tier': ModelTier.NONE,
            'description': 'Retry with backoff, reroute, or timeout adjustment',
            'max_patch_size': 0,  # No code changes
            'cost_estimate': 'free',
        },
        StepLayer.STRUCTURAL: {
            'handler': HandlerType.STRUCTURAL,
            'model_tier': ModelTier.SLM,
            'description': 'Fix malformed config/output against schema',
            'max_patch_size': 50,  # Small fixes only
            'cost_estimate': 'minimal',
        },
        StepLayer.SEMANTIC: {
            'handler': HandlerType.SEMANTIC,
            'model_tier': ModelTier.LLM,
            'description': 'Semantic repair with full reasoning',
            'max_patch_size': 200,  # Larger scope allowed
            'cost_estimate': 'standard',
        },
    }
    
    def __init__(self):
        """Initialize the repair router"""
        self.routing_decisions: list = []  # Audit trail
    
    def route(self, run: Run) -> Tuple[HandlerType, ModelTier, str]:
        """
        Deterministically route a run to a handler and model tier.
        
        Args:
            run: The Run to route
            
        Returns:
            Tuple of (HandlerType, ModelTier, reasoning)
        """
        # Get the classified layer from the most recent classification step
        classified_layer = self._get_classified_layer(run)
        
        if classified_layer is None:
            # Fallback: classify as semantic if not already classified
            classified_layer = StepLayer.SEMANTIC
            reason = "No classification step found; defaulting to semantic layer"
        else:
            reason = f"Using classified layer: {classified_layer.value}"
        
        # Look up routing
        routing = self.ROUTING_TABLE.get(
            classified_layer,
            self.ROUTING_TABLE[StepLayer.SEMANTIC]  # Fallback
        )
        
        handler = routing['handler']
        model_tier = routing['model_tier']
        
        # Check if we need to upgrade based on patch size
        model_tier = self._check_capacity_upgrade(run, classified_layer, model_tier)
        
        # Record decision
        decision = {
            'run_id': run.id,
            'layer': classified_layer.value,
            'handler': handler.value,
            'model_tier': model_tier.value,
            'reasoning': reason,
        }
        self.routing_decisions.append(decision)
        
        return handler, model_tier, reason
    
    def _get_classified_layer(self, run: Run) -> Optional[StepLayer]:
        """
        Extract the classified failure layer from run steps.
        
        Args:
            run: The Run to inspect
            
        Returns:
            StepLayer if found, None otherwise
        """
        # Look through steps in reverse (most recent first)
        for step in reversed(run.steps):
            if step.type == StepType.ATTRIBUTION and 'classified_layer' in step.output:
                try:
                    return StepLayer(step.output['classified_layer'])
                except (ValueError, KeyError):
                    pass
        
        return None
    
    def _check_capacity_upgrade(self, run: Run, layer: StepLayer, model_tier: ModelTier) -> ModelTier:
        """
        Check if we need to upgrade the model tier based on patch complexity.
        
        Capacity-based upgrades are different from reasoning-depth upgrades:
        - Capacity (SLM → LLM): The change is too large for the small model to handle
        - Reasoning (always LLM for semantic): The problem requires strong reasoning
        
        Args:
            run: The Run being routed
            layer: The failure layer
            model_tier: The initially selected model tier
            
        Returns:
            The same model tier, or an upgraded tier if necessary
        """
        # Estimate patch complexity
        diff_size = len(run.diff.split('\n'))
        file_count = len(set(
            line.split('/')[-1] for line in run.diff.split('\n') if line.startswith('+++')
        ))
        
        # For structural repairs, if the patch is large, upgrade to LLM
        if layer == StepLayer.STRUCTURAL and model_tier == ModelTier.SLM:
            if diff_size > 100 or file_count > 3:
                return ModelTier.LLM
        
        return model_tier
    
    def create_routing_step(self, run: Run, handler: HandlerType, model_tier: ModelTier, reason: str) -> Step:
        """
        Create a routing decision step to add to the run's audit trail.
        
        Args:
            run: The Run being routed
            handler: The selected handler
            model_tier: The selected model tier
            reason: Reasoning for the routing decision
            
        Returns:
            Step object representing the routing decision
        """
        step = Step(
            type=StepType.VERIFICATION,
            layer=self._get_classified_layer(run) or StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            input={
                'failure_layer': (self._get_classified_layer(run) or StepLayer.SEMANTIC).value,
                'patch_size': len(run.diff),
            },
            output={
                'selected_handler': handler.value,
                'selected_model_tier': model_tier.value,
                'reasoning': reason,
            }
        )
        return step
    
    def add_routing_to_run(self, run: Run) -> Tuple[HandlerType, ModelTier]:
        """
        Route a run and add the routing decision step to it.
        
        Args:
            run: The Run to route
            
        Returns:
            Tuple of (HandlerType, ModelTier)
        """
        handler, model_tier, reason = self.route(run)
        step = self.create_routing_step(run, handler, model_tier, reason)
        run.add_step(step)
        return handler, model_tier
    
    def get_routing_decisions(self) -> list:
        """
        Get all routing decisions made (for audit/debugging).
        
        Returns:
            List of routing decision records
        """
        return self.routing_decisions.copy()
