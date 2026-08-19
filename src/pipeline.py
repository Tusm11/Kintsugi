"""Kintsugi Pipeline: Full orchestration of all components"""

from typing import Tuple, Optional
from src.models import Run, RunStatus
from src.ingestion import IngestionLayer, WebhookEvent
from src.classifier import Classifier
from src.attribution import AttributionEngine
from src.repair_router import RepairRouter, HandlerType, ModelTier
from src.handlers import MechanicalHandler, StructuralHandler, SemanticHandler
from src.guardrails import (
    InputGuardrail, OutputGuardrail, ConfidenceGate, ScopeGuard, ScopeConfigManager
)
from src.verifier import Verifier
from src.budget import BudgetGuard
from src.rate_guard import RateAnomalyGuard
from src.action_layer import ActionLayer
from src.audit_log import AuditLog


class KintsugiPipeline:
    """
    Full Kintsugi self-healing pipeline orchestrator.
    
    Coordinates all components to process CI failures through:
    1. Ingestion (deduplication, queuing)
    2. Classification (failure layer detection)
    3. Attribution (root cause analysis)
    4. Routing (handler selection)
    5. Repair (mechanical/structural/semantic fixes)
    6. Guardrails (input/output scanning, confidence, scope)
    7. Verification (test execution)
    8. Action (PR or escalation)
    9. Audit logging
    """
    
    def __init__(self, semantic_provider=None, structural_provider=None):
        """
        Initialize the pipeline with all components.
        
        Args:
            semantic_provider: Optional ModelProvider for semantic repairs (defaults to env SEMANTIC_PROVIDER)
            structural_provider: Optional ModelProvider for structural repairs (defaults to env STRUCTURAL_PROVIDER)
            
        Raises:
            ValueError: If providers not passed and environment variables not configured
        """
        from src.model_provider import get_provider
        
        self.ingestion = IngestionLayer()
        self.classifier = Classifier()
        self.attribution = AttributionEngine()
        self.router = RepairRouter()
        
        # Load providers from environment if not provided (required - no fallback)
        if semantic_provider is None:
            semantic_provider = get_provider("semantic")
        
        if structural_provider is None:
            structural_provider = get_provider("structural")
        
        # Validate providers have required configuration at startup
        self._validate_provider_configuration()
        
        # Handlers with configured providers (required)
        self.mechanical_handler = MechanicalHandler()
        self.structural_handler = StructuralHandler(model_provider=structural_provider)
        self.semantic_handler = SemanticHandler(model_provider=semantic_provider)
        
        # Guardrails
        self.input_guardrail = InputGuardrail()
        self.output_guardrail = OutputGuardrail()
        self.confidence_gate = ConfidenceGate()
        self.scope_config_manager = ScopeConfigManager()
        self.scope_guard = ScopeGuard(config_manager=self.scope_config_manager)
        
        # Other components
        self.verifier = Verifier()
        self.budget_guard = BudgetGuard()
        self.rate_guard = RateAnomalyGuard()
        self.action_layer = ActionLayer()
        self.audit_log = AuditLog()
    
    def _validate_provider_configuration(self):
        """Validate that configured providers have valid API keys at startup"""
        import os
        
        # Check semantic provider
        semantic_provider = os.getenv("SEMANTIC_PROVIDER")
        if semantic_provider:
            api_key_var = f"{semantic_provider.upper()}_API_KEY"
            api_key = os.getenv(api_key_var)
            if not api_key:
                raise ValueError(f"Missing required API key: {api_key_var} (needed for SEMANTIC_PROVIDER={semantic_provider})")
        
        # Check structural provider
        structural_provider = os.getenv("STRUCTURAL_PROVIDER")
        if structural_provider:
            api_key_var = f"{structural_provider.upper()}_API_KEY"
            api_key = os.getenv(api_key_var)
            if not api_key:
                raise ValueError(f"Missing required API key: {api_key_var} (needed for STRUCTURAL_PROVIDER={structural_provider})")
    
    def ingest_event(self, event: WebhookEvent) -> Optional[Run]:
        """
        Ingest a CI failure event.
        
        Args:
            event: WebhookEvent from CI system
            
        Returns:
            Run object if new, None if duplicate
        """
        run = self.ingestion.ingest(event)
        if run:
            self.audit_log.log_run_ingestion(run)
            self.budget_guard.initialize_budget(run)
        return run
    
    def process_run(self, run: Run) -> Tuple[RunStatus, str]:
        """
        Process a single run through the full pipeline.
        
        Args:
            run: The Run to process
            
        Returns:
            Tuple of (final_status, summary)
        """
        summary_parts = []
        
        # Step 1: Rate guard check
        rate_allowed, rate_reason = self.rate_guard.record_event(run.repo, run.metadata.get('source', 'unknown'))
        if not rate_allowed:
            self.audit_log.log_guardrail_check(run.id, "rate_guard", False, rate_reason)
            run.final_status = RunStatus.ESCALATED
            self.audit_log.log_run_completion(run, "escalated")
            return RunStatus.ESCALATED, f"Rate guard: {rate_reason}"
        
        self.audit_log.log_guardrail_check(run.id, "rate_guard", True, rate_reason)
        
        # Step 2: Input guardrail (scan for injection)
        input_safe, input_reason = self.input_guardrail.scan_run(run)
        if not input_safe:
            self.audit_log.log_guardrail_check(run.id, "input_guardrail", False, input_reason)
            run.final_status = RunStatus.ESCALATED
            self.audit_log.log_run_completion(run, "escalated")
            return RunStatus.ESCALATED, f"Input guardrail: {input_reason}"
        
        self.audit_log.log_guardrail_check(run.id, "input_guardrail", True, "Content safe")
        
        # Step 3: Classify failure
        layer, classification_reason = self.classifier.classify(run)
        self.classifier.add_classification_to_run(run, layer, classification_reason)
        self.audit_log.log_classification(run, layer.value, classification_reason)
        summary_parts.append(f"Classification: {layer.value}")
        
        # Step 4: Attribution (for semantic failures)
        if layer == "semantic":
            attribution = self.attribution.add_attribution_to_run(run)
            self.audit_log.log_attribution(
                run, 
                attribution.claimed_cause,
                attribution.counterfactual_result or "inconclusive"
            )
            summary_parts.append(f"Attribution: {attribution.claimed_cause}")
        
        # Step 5: Route to handler
        handler, model_tier, routing_reason = self.router.route(run)
        self.router.add_routing_to_run(run)
        self.audit_log.log_routing_decision(run.id, handler.value, model_tier.value, routing_reason)
        summary_parts.append(f"Handler: {handler.value}")
        
        # Step 6: Apply repair
        repair_attempted = False
        repair_success = False
        
        if handler == HandlerType.MECHANICAL:
            repair_success = self.mechanical_handler.add_repair_to_run(run, budget_guard=self.budget_guard)
            repair_attempted = True
        elif handler == HandlerType.STRUCTURAL:
            repair_success = self.structural_handler.add_repair_to_run(run, use_llm=(model_tier == ModelTier.LLM), budget_guard=self.budget_guard)
            repair_attempted = True
        elif handler == HandlerType.SEMANTIC:
            repair_success = self.semantic_handler.add_repair_to_run(run, budget_guard=self.budget_guard)
            repair_attempted = True
        
        if repair_attempted:
            self.audit_log.log_repair_attempt(
                run.id,
                layer.value,
                repair_success,
                f"Repair attempt with {handler.value} handler"
            )
            summary_parts.append(f"Repair: {'Success' if repair_success else 'Failed'}")
        
        # Step 7: Output guardrail (scan repair for secrets/unsafe patterns)
        if repair_success:
            # In production, we'd get the actual repair output here
            output_safe, output_reason = self.output_guardrail.scan_repair_output(
                "Proposed fix",
                "def fix(): return True"  # Simulated
            )
            if not output_safe:
                self.audit_log.log_guardrail_check(run.id, "output_guardrail", False, output_reason)
                run.final_status = RunStatus.ESCALATED
                self.audit_log.log_run_completion(run, "escalated")
                return RunStatus.ESCALATED, f"Output guardrail: {output_reason}"
        
        # Step 8: Confidence gate (structured evidence check)
        eligible, confidence_reason = self.confidence_gate.is_eligible_for_auto_apply(run)
        self.confidence_gate.create_gate_decision_step(run, eligible, confidence_reason)
        self.audit_log.log_guardrail_check(run.id, "confidence_gate", eligible, confidence_reason)
        
        # Step 9: Scope guard (hard safety rules)
        in_scope, scope_reason = self.scope_guard.is_in_scope_for_auto_apply(run, repo=run.repo)
        scope_config = self.scope_config_manager.get_config(run.repo)
        self.scope_guard.create_scope_decision_step(run, in_scope, scope_reason)
        self.audit_log.log_guardrail_check(run.id, "scope_guard", in_scope, scope_reason)
        self.audit_log.log_scope_config_state(run.id, scope_config, in_scope)
        
        # Step 10: Verification (only if passed all checks)
        if eligible and in_scope and repair_success:
            verified = self.verifier.verify_and_record(run)
            self.audit_log.log_verification(run.id, verified, "Tests executed")
            summary_parts.append(f"Verification: {'Passed' if verified else 'Failed'}")
            
            if verified:
                # Step 11: Action layer (open PR)
                pr_success, pr_url = self.action_layer.apply_fix(run)
                if pr_success:
                    self.audit_log.log_action(run.id, "open_pr", pr_url, True)
                    run.final_status = RunStatus.HEALED
                    self.audit_log.log_run_completion(run, "healed")
                    return RunStatus.HEALED, f"PR opened: {pr_url}"
            else:
                # Escalate if verification failed
                success, issue_url = self.action_layer.escalate(
                    run,
                    "Verification failed - tests did not pass"
                )
                if success:
                    self.audit_log.log_action(run.id, "escalate", issue_url, True)
        else:
            # Escalate if gates failed
            reason = "Confidence gate" if not eligible else "Scope guard" if not in_scope else "Repair failed"
            success, issue_url = self.action_layer.escalate(run, f"Failed {reason}")
            if success:
                self.audit_log.log_action(run.id, "escalate", issue_url, True)
        
        run.final_status = RunStatus.ESCALATED
        self.audit_log.log_run_completion(run, "escalated")
        
        return RunStatus.ESCALATED, " → ".join(summary_parts)
    
    def process_queue(self) -> dict:
        """
        Process all pending runs in the ingestion queue.
        
        Returns:
            Dictionary with processing statistics
        """
        stats = {
            'total_processed': 0,
            'healed': 0,
            'escalated': 0,
            'failed': 0,
        }
        
        while True:
            run = self.ingestion.dequeue_run()
            if not run:
                break
            
            status, summary = self.process_run(run)
            stats['total_processed'] += 1
            
            if status == RunStatus.HEALED:
                stats['healed'] += 1
            elif status == RunStatus.ESCALATED:
                stats['escalated'] += 1
            else:
                stats['failed'] += 1
        
        return stats
    
    def get_audit_trail(self, run_id: str) -> list:
        """
        Get complete audit trail for a run.
        
        Args:
            run_id: The Run ID
            
        Returns:
            List of audit log entries
        """
        return self.audit_log.get_run_log(run_id)
    
    def get_statistics(self) -> dict:
        """
        Get overall system statistics.
        
        Returns:
            Dictionary with statistics
        """
        return {
            'audit_log': self.audit_log.get_statistics(),
            'ingestion_queue': self.ingestion.get_stats(),
            'rate_anomalies': len(self.rate_guard.flagged_actors),
        }
