"""Kintsugi Pipeline: Full orchestration of all components"""

from typing import Tuple, Optional, List
from src.models import Run, RunStatus, StepLayer, StepType, Attribution
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
from src.attribution_similarity import MatchTier
from src.fix_cache import FixCache, CacheLookup
from src.context_buckets import ContextBucketBuilder, bucket_tier_for_attempt, checkout_code_context
from src.cache_metrics import CacheMetrics
from src.sandbox import RepoSandbox
from src.github_client import GitHubClient


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
    
    def __init__(
        self,
        semantic_provider=None,
        structural_provider=None,
        fix_cache: Optional[FixCache] = None,
        use_fix_cache: bool = True,
        context_builder: Optional[ContextBucketBuilder] = None,
        sandbox: Optional[RepoSandbox] = None,
        github: Optional[GitHubClient] = None,
    ):
        """
        Initialize the pipeline with all components.

        Args:
            semantic_provider: Optional ModelProvider for semantic repairs (defaults to env SEMANTIC_PROVIDER)
            structural_provider: Optional ModelProvider for structural repairs (defaults to env STRUCTURAL_PROVIDER)
            fix_cache: Optional FixCache. If omitted and use_fix_cache is True,
                one is built from the environment (FixCache.from_env: Redis if
                REDIS_URL is set, else in-memory; FIX_CACHE_ENABLED=false disables).
            use_fix_cache: Set False to run the semantic path with no cache at all
                (context buckets still apply).
            context_builder: Optional ContextBucketBuilder (e.g. with a
                code_context_provider that reads adjacent functions from a checkout).
            sandbox: Optional RepoSandbox (local checkout paths + test command).
                Default reads KINTSUGI_REPO_PATH_* / KINTSUGI_REPO_PATHS and
                KINTSUGI_TEST_COMMAND from the environment.
            github: Optional GitHubClient for PRs/issues (default: GITHUB_TOKEN).

        Raises:
            ValueError: If providers not passed and environment variables not configured
        """
        from src.model_provider import get_provider
        
        self.ingestion = IngestionLayer()
        self.classifier = Classifier()
        # One sandbox (local checkout + test command) shared by every component that runs code.
        self.sandbox = sandbox or RepoSandbox()
        self.router = RepairRouter()
        
        # Load providers from environment if not provided (required - no fallback).
        # The env/API-key check applies only to providers built from the
        # environment; explicitly passed providers carry their own config.
        providers_from_env = semantic_provider is None or structural_provider is None
        if semantic_provider is None:
            semantic_provider = get_provider("semantic")
        
        if structural_provider is None:
            structural_provider = get_provider("structural")
        
        # Diagnosis uses the same model as semantic repair.
        self.attribution = AttributionEngine(sandbox=self.sandbox, model_provider=semantic_provider)
        
        if providers_from_env:
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
        self.verifier = Verifier(sandbox=self.sandbox)
        self.budget_guard = BudgetGuard()
        self.rate_guard = RateAnomalyGuard()
        self.action_layer = ActionLayer(github=github, sandbox=self.sandbox)
        self.audit_log = AuditLog()

        # v2: fix-cache + context-bucket tiering for semantic repairs
        if fix_cache is not None:
            self.fix_cache: Optional[FixCache] = fix_cache
        elif use_fix_cache:
            self.fix_cache = FixCache.from_env()
        else:
            self.fix_cache = None
        # Metrics are shared with the cache when there is one, so bucket usage and
        # cache hits land in the same counters/JSONL sink.
        self.cache_metrics = self.fix_cache.metrics if self.fix_cache else CacheMetrics()
        # Bucket 2's "surrounding code": read from the configured checkout by default.
        self.context_builder = context_builder or ContextBucketBuilder(
            code_context_provider=checkout_code_context(self.sandbox)
        )

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
        
        # Step 4 (v2): semantic failures take the fix-cache + context-bucket path.
        # Mechanical/structural failures continue through the v1 flow below unchanged.
        if layer == StepLayer.SEMANTIC:
            return self._process_semantic_run(run, summary_parts)

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
            # Scan what the handler actually produced: the structural patch, or
            # for a mechanical retry (no code) the recorded action text.
            last_output = run.steps[-1].output if run.steps else {}
            produced = (last_output.get('repair_output') or {}).get('fix_patch') or last_output.get('action_taken', '')
            output_safe, output_reason = self.output_guardrail.scan_repair_output(
                last_output.get('repair_description', last_output.get('action_taken', '')),
                produced,
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
    
    # ------------------------------------------------------------------
    # v2 semantic path: fix-cache + context-bucket tiering
    # ------------------------------------------------------------------

    def _process_semantic_run(self, run: Run, summary_parts: List[str]) -> Tuple[RunStatus, str]:
        """
        Semantic repair with fix-cache reuse and bucketed retries.

        Order of attempts (each falls through to the next only on a miss or a
        failed re-verification):

          A. Signature lookup (pre-attribution). EXACT hit -> reuse cached
             attribution + patch; Attribution Engine and generation are skipped.
          B. Fresh attribution, then fingerprint/similarity lookup.
             EXACT -> reuse cached patch, generation skipped.
             NEAR  -> cached attribution becomes the bucket-1 seed.
          C. Generation loop: attempt N gets context bucket N+1, bounded by the
             per-layer semantic retry budget.

        Every patch, cached or generated, passes Output Guardrail -> Confidence
        Gate -> Scope Guard -> Verifier. Only a Verifier pass writes the cache.
        """
        # AX-VERIFIED: a cached patch is never handed to the Action Layer without a
        # Verifier pass in the same Run, and a reused patch that fails verification
        # is busted from the cache before generation continues.
        # (tests/test_pipeline_fix_cache.py::TestExactReuse::test_exact_reuse_still_runs_gates_and_verifier,
        # ::test_failed_reverification_busts_and_falls_back_to_generation)
        cache = self.fix_cache
        routed = False

        # --- A. pre-attribution signature lookup -------------------------
        if cache is not None:
            sig_lookup = cache.lookup_by_signature(run)
            self.audit_log.log_cache_event(run.id, "lookup", self._lookup_details(sig_lookup))
            if sig_lookup.is_exact:
                cached_attr = sig_lookup.entry.attribution_obj()
                self._add_cached_attribution_step(run, cached_attr, sig_lookup)
                summary_parts.append(f"Attribution: {cached_attr.claimed_cause} (fix-cache, signature match)")
                self._route(run, summary_parts)
                routed = True
                outcome = self._attempt_cached_fix(run, sig_lookup, summary_parts)
                if outcome is not None:
                    return outcome

        # --- B. fresh attribution + fingerprint/similarity lookup --------
        attribution = self.attribution.add_attribution_to_run(run)
        self.audit_log.log_attribution(
            run,
            attribution.claimed_cause,
            attribution.counterfactual_result or "inconclusive"
        )
        summary_parts.append(f"Attribution: {attribution.claimed_cause}")
        if not routed:
            self._route(run, summary_parts)

        lookup = CacheLookup(MatchTier.NONE, reason="fix-cache disabled")
        if cache is not None:
            lookup = cache.lookup_by_attribution(run.repo, attribution, run_id=run.id)
            self.audit_log.log_cache_event(run.id, "lookup", self._lookup_details(lookup))
            if lookup.is_exact:
                outcome = self._attempt_cached_fix(run, lookup, summary_parts)
                if outcome is not None:
                    return outcome
                lookup = CacheLookup(MatchTier.NONE, reason="exact entry failed re-verification and was busted")

        # --- C. generation with context buckets ---------------------------
        return self._semantic_generation_loop(run, attribution, lookup, summary_parts)

    def _route(self, run: Run, summary_parts: List[str]) -> None:
        handler, model_tier, routing_reason = self.router.route(run)
        self.router.add_routing_to_run(run)
        self.audit_log.log_routing_decision(run.id, handler.value, model_tier.value, routing_reason)
        summary_parts.append(f"Handler: {handler.value}")

    @staticmethod
    def _lookup_details(lookup: CacheLookup) -> dict:
        return {
            'path': lookup.path,
            'tier': lookup.tier.value,
            'score': lookup.score,
            'breakdown': lookup.breakdown,
            'fingerprint': lookup.entry.fingerprint if lookup.entry else None,
            'reason': lookup.reason,
        }

    def _add_cached_attribution_step(self, run: Run, attribution: Attribution, lookup: CacheLookup) -> None:
        """Record the cached attribution as this Run's attribution (signature path)."""
        step = self.attribution.create_attribution_step(run, attribution)
        step.output['source'] = 'fix_cache'
        step.output['cache_fingerprint'] = lookup.entry.fingerprint
        step.output['cache_lookup_path'] = lookup.path
        run.add_step(step)
        self.audit_log.log_attribution(
            run,
            attribution.claimed_cause,
            attribution.counterfactual_result or "inconclusive"
        )

    def _check_gates(self, run: Run) -> Tuple[bool, str, bool, str]:
        """Confidence Gate + Scope Guard, with their decision steps and audit entries.

        The decision steps are appended to the Run: ActionLayer.should_apply_fix
        looks for them and refuses to open a PR without both. (v1 built these
        steps but never added them, so the Action Layer always refused.)
        """
        eligible, confidence_reason = self.confidence_gate.is_eligible_for_auto_apply(run)
        run.add_step(self.confidence_gate.create_gate_decision_step(run, eligible, confidence_reason))
        self.audit_log.log_guardrail_check(run.id, "confidence_gate", eligible, confidence_reason)

        in_scope, scope_reason = self.scope_guard.is_in_scope_for_auto_apply(run, repo=run.repo)
        scope_config = self.scope_config_manager.get_config(run.repo)
        run.add_step(self.scope_guard.create_scope_decision_step(run, in_scope, scope_reason))
        self.audit_log.log_guardrail_check(run.id, "scope_guard", in_scope, scope_reason)
        self.audit_log.log_scope_config_state(run.id, scope_config, in_scope)
        return eligible, confidence_reason, in_scope, scope_reason

    def _escalate(self, run: Run, reason: str, summary_parts: List[str]) -> Tuple[RunStatus, str]:
        # Logged whether or not the GitHub issue was posted (e.g. no GITHUB_TOKEN):
        # the escalation itself must never be silent.
        success, issue_url_or_reason = self.action_layer.escalate(run, reason)
        self.audit_log.log_action(run.id, "escalate", issue_url_or_reason, success)
        run.final_status = RunStatus.ESCALATED
        self.audit_log.log_run_completion(run, "escalated")
        return RunStatus.ESCALATED, " → ".join(summary_parts + [reason])

    def _apply_and_finish(self, run: Run, summary_parts: List[str]) -> Tuple[RunStatus, str]:
        pr_success, pr_url = self.action_layer.apply_fix(run)
        if pr_success:
            self.audit_log.log_action(run.id, "open_pr", pr_url, True)
            run.final_status = RunStatus.HEALED
            self.audit_log.log_run_completion(run, "healed")
            return RunStatus.HEALED, f"PR opened: {pr_url}"
        return self._escalate(run, "Action layer could not open PR", summary_parts)

    def _attempt_cached_fix(
        self, run: Run, lookup: CacheLookup, summary_parts: List[str]
    ) -> Optional[Tuple[RunStatus, str]]:
        """
        Push a cached patch through the unchanged gate/verify chain.

        Returns a terminal (status, summary) when the Run is decided (healed, or
        escalated by a gate), or None when the caller should fall through to
        fresh attribution/generation (cached patch rejected or failed verification).
        """
        entry = lookup.entry
        repair_step = self.semantic_handler.add_cached_repair_to_run(run, entry, lookup.path)
        self.audit_log.log_repair_attempt(
            run.id, "semantic", True,
            f"Reused cached fix {entry.fingerprint[:12]} ({lookup.path} match, hit #{entry.hit_count})"
        )
        summary_parts.append(f"Repair: fix-cache reuse ({lookup.path})")

        # Output guardrail on the real cached patch. A rejection here means the
        # guard rules changed since the fix was cached: bust it and regenerate.
        output_safe, output_reason = self.output_guardrail.scan_repair_output(
            repair_step.output['repair_description'], entry.fix_patch
        )
        if not output_safe:
            self.audit_log.log_guardrail_check(run.id, "output_guardrail", False, output_reason)
            self.fix_cache.bust(run.repo, entry.fingerprint, "output_guardrail", run_id=run.id)
            self.audit_log.log_cache_event(run.id, "bust", {'fingerprint': entry.fingerprint, 'reason': output_reason})
            summary_parts.append("Cached fix rejected by output guardrail (busted)")
            return None

        eligible, _, in_scope, _ = self._check_gates(run)
        if not (eligible and in_scope):
            # Gate decisions are about this Run's attribution/diff, not the patch;
            # regenerating would hit the same gate, so escalate as v1 does. The
            # cache entry is not busted: nothing showed the patch is wrong.
            return self._escalate(run, f"Failed {'Confidence gate' if not eligible else 'Scope guard'}", summary_parts)

        verified = self.verifier.verify_and_record(run)
        self.audit_log.log_verification(run.id, verified, "Tests executed (cached fix)")
        self.fix_cache.record_reuse_result(run, lookup, verified)
        self.audit_log.log_cache_event(
            run.id, "reuse", {'fingerprint': entry.fingerprint, 'path': lookup.path, 'verified': verified}
        )
        if verified:
            summary_parts.append("Verification: Passed")
            return self._apply_and_finish(run, summary_parts)
        summary_parts.append("Verification: Failed (cached fix busted)")
        return None

    def _semantic_generation_loop(
        self, run: Run, attribution: Attribution, lookup: CacheLookup, summary_parts: List[str]
    ) -> Tuple[RunStatus, str]:
        """
        Generate fixes with widening context buckets until one verifies or the
        semantic retry budget runs out.

        Retried: model call failed, or Verifier failed the patch.
        Not retried (escalate immediately): Output Guardrail rejection, Confidence
        Gate or Scope Guard failure — more context does not change those.
        """
        seed = lookup.entry if lookup.is_near else None
        seed_attr = seed.attribution_obj() if seed else None
        gates_checked = False
        last_failure = "Repair failed"
        attempt = 0

        while True:
            tier = bucket_tier_for_attempt(attempt)
            bucket = self.context_builder.build(
                tier, run, attribution,
                seed_attribution=seed_attr,
                seed_fingerprint=seed.fingerprint if seed else None,
                seed_score=lookup.score if seed else None,
            )
            steps_before = len(run.steps)
            repair_success = self.semantic_handler.add_repair_to_run(
                run, budget_guard=self.budget_guard, context_bucket=bucket, attempt_number=attempt + 1
            )
            step_added = len(run.steps) > steps_before
            tokens = run.steps[-1].cost.tokens_used if step_added else 0
            self.cache_metrics.record(
                "bucket.used", run_id=run.id, repo=run.repo, bucket_tier=tier, attempt=attempt + 1,
                seeded=bucket.seeded, tokens_used=tokens, approx_context_tokens=bucket.approx_tokens,
                truncated=bucket.truncated, generation_ok=repair_success, budget_refused=not step_added,
            )
            self.audit_log.log_cache_event(run.id, "bucket", dict(bucket.summary(), attempt=attempt + 1))

            if not step_added:
                # Budget guard refused the attempt before any model call.
                if attempt == 0:
                    last_failure = "Repair failed"
                break

            self.audit_log.log_repair_attempt(
                run.id, "semantic", repair_success,
                f"Repair attempt {attempt + 1} with semantic handler (context bucket {tier}"
                f"{', seeded' if bucket.seeded else ''})"
            )
            summary_parts.append(f"Repair: {'Success' if repair_success else 'Failed'} (bucket {tier})")

            if repair_success:
                repair_output = run.steps[-1].output.get('repair_output', {})
                fix_patch = repair_output.get('fix_patch', '')
                output_safe, output_reason = self.output_guardrail.scan_repair_output(
                    run.steps[-1].output.get('repair_description', ''), fix_patch
                )
                if not output_safe:
                    self.audit_log.log_guardrail_check(run.id, "output_guardrail", False, output_reason)
                    run.final_status = RunStatus.ESCALATED
                    self.audit_log.log_run_completion(run, "escalated")
                    return RunStatus.ESCALATED, f"Output guardrail: {output_reason}"

                eligible, _, in_scope, _ = self._check_gates(run)
                gates_checked = True
                if not (eligible and in_scope):
                    return self._escalate(
                        run, f"Failed {'Confidence gate' if not eligible else 'Scope guard'}", summary_parts
                    )

                verified = self.verifier.verify_and_record(run)
                self.audit_log.log_verification(run.id, verified, f"Tests executed (bucket {tier})")
                summary_parts.append(f"Verification: {'Passed' if verified else 'Failed'}")
                if verified:
                    if self.fix_cache is not None:
                        fp = self.fix_cache.store(
                            run, attribution, fix_patch,
                            verification_result=run.steps[-1].output.get('test_results', {}),
                        )
                        self.audit_log.log_cache_event(run.id, "store", {'fingerprint': fp, 'stored': fp is not None})
                    return self._apply_and_finish(run, summary_parts)
                last_failure = "Verification failed - tests did not pass"
            else:
                last_failure = "Repair failed"

            attempt += 1
            if not self.budget_guard.can_retry(run.id, "semantic"):
                break

        if not gates_checked:
            # Keep v1's audit shape: gate decisions are recorded even when no
            # repair got far enough to need them, and name the first gate that fails.
            eligible, _, in_scope, _ = self._check_gates(run)
            if not eligible:
                last_failure = "Confidence gate"
            elif not in_scope:
                last_failure = "Scope guard"
            return self._escalate(run, f"Failed {last_failure}", summary_parts)
        return self._escalate(run, last_failure, summary_parts)

    def get_cache_metrics(self) -> dict:
        """Fix-cache / context-bucket counters and rates (see src/cache_metrics.py)."""
        return self.cache_metrics.snapshot()

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
            'fix_cache': self.get_cache_metrics(),
        }
