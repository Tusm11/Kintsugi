"""Repair Handlers: Mechanical, Structural, and Semantic"""

import re
import time
import yaml
import json
from typing import Dict, Any, Optional, List, Tuple
from src.models import Run, Step, StepType, StepLayer, StepStatus, Cost
from src.model_provider import ModelProvider
from src.prompts import SemanticRepairPrompt, StructuralRepairPrompt


class MechanicalHandler:
    """
    Handles mechanical (infrastructure) failures deterministically.
    
    No LLM involved and no code is changed. Every matched strategy (timeout,
    rate limit, 503, connection reset) resolves to the same real action: a
    retry, i.e. the Verifier re-runs the test suite unchanged at the failing
    commit (a mechanical REPAIR step carries no patch). The strategy only
    decides whether the failure looks transient enough to be worth retrying
    and records why.
    """
    
    def __init__(self, max_retries: Optional[int] = None, initial_backoff_ms: int = 100):
        """
        Initialize mechanical handler.
        
        Args:
            max_retries: Optional retry limit for mechanical guidance
            initial_backoff_ms: Initial backoff in milliseconds
        """
        self.max_retries = max_retries
        self.initial_backoff_ms = initial_backoff_ms
    
    def handle(self, run: Run) -> Tuple[bool, str]:
        """
        Attempt deterministic mechanical repair.
        
        Args:
            run: The Run to handle
            
        Returns:
            Tuple of (success, action_description)
        """
        logs = run.failure_logs
        
        # Strategy 1: Timeout
        if "timeout" in logs.lower():
            return self._handle_timeout(run)
        
        # Strategy 2: Rate limit
        if "rate" in logs.lower() and "limit" in logs.lower():
            return self._handle_rate_limit(run)
        
        # Strategy 3: Service unavailable - retry
        if "503" in logs or "unavailable" in logs.lower():
            return self._handle_service_unavailable(run)
        
        # Strategy 4: Connection reset/refused
        if "connection" in logs.lower() and ("reset" in logs.lower() or "refused" in logs.lower()):
            return self._handle_connection_error(run)
        
        return False, "No mechanical repair strategy matched"
    
    def _handle_timeout(self, run: Run) -> Tuple[bool, str]:
        """Timeout: treat as transient and retry"""
        action = "Retry: re-run the test suite unchanged (timeout looks transient)"
        return True, action
    
    def _handle_rate_limit(self, run: Run) -> Tuple[bool, str]:
        """Rate limit: treat as transient and retry"""
        action = "Retry: re-run the test suite unchanged (rate limit looks transient)"
        return True, action
    
    def _handle_service_unavailable(self, run: Run) -> Tuple[bool, str]:
        """Service unavailable: treat as transient and retry"""
        action = "Retry: re-run the test suite unchanged (service was unavailable)"
        return True, action
    
    def _handle_connection_error(self, run: Run) -> Tuple[bool, str]:
        """Connection error: treat as transient and retry"""
        action = "Retry: re-run the test suite unchanged (transient network error)"
        return True, action
    
    def create_repair_step(self, run: Run, success: bool, action: str) -> Step:
        """
        Create a repair step for the run's audit trail.
        
        Args:
            run: The Run being repaired
            success: Whether repair was attempted
            action: Description of action taken
            
        Returns:
            Step object representing the repair
        """
        step = Step(
            type=StepType.REPAIR,
            layer=StepLayer.MECHANICAL,
            status=StepStatus.SUCCESS if success else StepStatus.FAILED,
            input={'failure_type': 'mechanical'},
            output={
                'action_taken': action,
                'success': success,
            },
            cost=Cost(tokens_used=0, wall_clock_ms=0)
        )
        return step
    
    def add_repair_to_run(self, run: Run, budget_guard=None) -> bool:
        """
        Attempt repair and add step to run.
        
        Args:
            run: The Run to repair
            budget_guard: Optional BudgetGuard for retry tracking
            
        Returns:
            Whether repair was successful
        """
        # Record mechanical retry attempt if budget guard provided
        if budget_guard:
            retry_success, retry_msg = budget_guard.record_retry(run.id, "mechanical")
            if not retry_success:
                return False  # Mechanical retry budget exhausted
        
        success, action = self.handle(run)
        step = self.create_repair_step(run, success, action)
        run.add_step(step)
        return success
    

class StructuralHandler:
    """
    Handles structural (malformed config/output) failures using a model provider.
    
    Fixes include:
    - YAML formatting corrections
    - JSON schema validation and fixes
    - Configuration restructuring
    - Lint/format corrections
    
    Can use any model provider configured by user.
    """
    
    def __init__(self, model_provider: Optional[ModelProvider] = None):
        """
        Initialize structural handler.
        
        Args:
            model_provider: ModelProvider to use for repair (required; there is no
                silent mock fallback. Tests pass a MockProvider explicitly.)
        
        Raises:
            ValueError: If no provider is given
        """
        if model_provider is None:
            raise ValueError("StructuralHandler requires a model_provider")
        self.model_provider = model_provider
    
    def handle(self, run: Run, budget_tokens: int = 1000) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Attempt structural repair using the configured model provider.
        
        Args:
            run: The Run to handle
            budget_tokens: Maximum tokens to spend on this repair
            
        Returns:
            Tuple of (success, description, repair_output)
        """
        logs = run.failure_logs
        
        # Identify issue type
        issue_type = self._identify_issue_type(logs)
        
        if not issue_type:
            return False, "Could not identify structural issue type", {}
        
        # Generate prompt for the model
        prompt = StructuralRepairPrompt.generate(
            failure_logs=logs,
            diff=run.diff,
            issue_type=issue_type,
        )
        
        # Call the model provider WITH RETRY
        success, response = self.model_provider.call_with_retry(
            prompt=prompt,
            budget_tokens=budget_tokens,
            temperature=0.0,  # Low temperature for deterministic structural fixes
        )
        
        if not success:
            return False, f"Model call failed after {response.retry_count} retries: {response.content}", {}
        
        # Parse model response
        fix_suggestion = self._parse_model_response(response.content, issue_type, response)
        
        return True, fix_suggestion['description'], fix_suggestion
    
    def _identify_issue_type(self, logs: str) -> Optional[str]:
        """Identify what type of structural issue this is"""
        logs_lower = logs.lower()
        
        if "yaml" in logs_lower or "yml" in logs_lower:
            return "yaml_error"
        elif "json" in logs_lower:
            return "json_error"
        elif "lint" in logs_lower or "eslint" in logs_lower:
            return "lint_error"
        elif "compile" in logs_lower or "syntax" in logs_lower:
            return "compilation_error"
        
        return None
    
    def _parse_model_response(self, content: str, issue_type: str, response) -> Dict[str, Any]:
        """
        Parse model response into structured format.
        
        Args:
            content: The raw model response
            issue_type: Type of structural issue
            response: ModelResponse object with token counts
            
        Returns:
            Dictionary with parsed fix proposal
        """
        fix = {
            'description': content[:200],  # First 200 chars as description
            'reasoning': content,
            'issue_type': issue_type,
            'model_provider': self.model_provider.get_name(),
            'tokens_used': response.total_tokens,
            'cost': response.total_cost,
            'full_response': content,
            'fix_patch': extract_section(content, "SOLUTION", ("VERIFICATION", "PROBLEM")),
        }
        
        return fix
    
    def create_repair_step(self, run: Run, success: bool, description: str, output: Dict[str, Any]) -> Step:
        """
        Create a repair step for the run's audit trail.
        
        Args:
            run: The Run being repaired
            success: Whether repair was successful
            description: Description of repair
            output: Repair output/suggestions
            
        Returns:
            Step object representing the repair
        """
        tokens_used = output.get('tokens_used', 0)
        
        step = Step(
            type=StepType.REPAIR,
            layer=StepLayer.STRUCTURAL,
            status=StepStatus.SUCCESS if success else StepStatus.FAILED,
            input={
                'failure_logs_snippet': run.failure_logs[:300],
                'issue_type': output.get('issue_type', 'unknown'),
                'model_provider': output.get('model_provider', 'unknown'),
            },
            output={
                'repair_description': description,
                'repair_output': output,
                'success': success,
            },
            cost=Cost(tokens_used=tokens_used, wall_clock_ms=500)
        )
        return step
    
    def add_repair_to_run(self, run: Run, use_llm: bool = False, budget_guard=None) -> bool:
        """
        Attempt repair and add step to run.
        
        Args:
            run: The Run to repair
            use_llm: Whether to use LLM (True) or SLM (False)
            budget_guard: Optional BudgetGuard for retry tracking
            
        Returns:
            Whether repair was successful
        """
        # Record structural retry attempt if budget guard provided
        if budget_guard:
            retry_success, retry_msg = budget_guard.record_retry(run.id, "structural")
            if not retry_success:
                return False  # Structural retry budget exhausted
        
        success, description, output = self.handle(run)
        step = self.create_repair_step(run, success, description, output)
        run.add_step(step)
        return success


class SemanticHandler:
    """
    Handles semantic (logic) failures using a model provider.
    
    Uses the reasoning capacity of a language model (configurable by user).
    Can use any model provider (OpenAI, Groq, Anthropic, etc.).
    
    Input to model:
    - Diff of changes
    - Test failure logs
    - Attribution evidence (what the root cause is suspected to be)
    
    Output expected:
    - Code fix that addresses the root cause
    - Explanation of why this fix resolves the failure
    """
    
    def __init__(self, model_provider: Optional[ModelProvider] = None):
        """
        Initialize semantic handler.
        
        Args:
            model_provider: ModelProvider to use for repair (required; there is no
                silent mock fallback. Tests pass a MockProvider explicitly.)
        
        Raises:
            ValueError: If no provider is given
        """
        if model_provider is None:
            raise ValueError("SemanticHandler requires a model_provider")
        self.model_provider = model_provider
    
    def handle(
        self,
        run: Run,
        budget_tokens: int = 2000,
        context_bucket=None,
        attempt_number: int = 1,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Attempt semantic repair using the configured model provider.

        Args:
            run: The Run to handle
            budget_tokens: Maximum tokens to spend on this repair
            context_bucket: Optional v2 ContextBucket. When given, the prompt is
                built from the bucket's bounded context instead of the full
                logs/diff (see src/context_buckets.py).
            attempt_number: 1-based semantic attempt number (prompt wording only)

        Returns:
            Tuple of (success, description, repair_output)
        """
        # Get attribution if available
        attribution_step = self._get_attribution_step(run)

        if not attribution_step:
            return False, "No attribution step found", {}

        attribution = attribution_step.attribution

        # Generate prompt for the model
        if context_bucket is not None:
            prompt = SemanticRepairPrompt.generate_from_bucket(
                bucket_text=context_bucket.render(),
                bucket_tier=context_bucket.tier,
                attempt_number=attempt_number,
                seeded=context_bucket.seeded,
            )
        else:
            prompt = SemanticRepairPrompt.generate(
                failure_logs=run.failure_logs,
                diff=run.diff,
                commit_message=run.commit_message,
                attributed_cause=attribution.claimed_cause,
                evidence_for=attribution.evidence_for,
                evidence_against=attribution.evidence_against,
            )
        
        # Call the model provider WITH RETRY
        success, response = self.model_provider.call_with_retry(
            prompt=prompt,
            budget_tokens=budget_tokens,
            temperature=0.7,
        )
        
        if not success:
            return False, f"Model call failed after {response.retry_count} retries: {response.content}", {}
        
        # Parse model response
        fix_suggestion = self._parse_model_response(response.content, response)
        
        return True, fix_suggestion['description'], fix_suggestion
    
    def _get_attribution_step(self, run: Run) -> Optional[Step]:
        """Get the most recent attribution step from the run"""
        for step in reversed(run.steps):
            if step.type == StepType.ATTRIBUTION and step.attribution:
                return step
        return None
    
    def _parse_model_response(self, content: str, response) -> Dict[str, Any]:
        """
        Parse model response into structured format.
        
        Args:
            content: The raw model response
            response: ModelResponse object with token counts
            
        Returns:
            Dictionary with parsed fix proposal
        """
        fix = {
            'description': content[:200],  # First 200 chars as description
            'reasoning': content,
            'model_provider': self.model_provider.get_name(),
            'tokens_used': response.total_tokens,
            'cost': response.total_cost,
            'confidence': 'high',
            'full_response': content,
            'fix_patch': extract_fix_patch(content),
        }

        return fix
    
    def create_repair_step(self, run: Run, success: bool, description: str, output: Dict[str, Any]) -> Step:
        """
        Create a repair step for the run's audit trail.
        
        Args:
            run: The Run being repaired
            success: Whether repair was successful
            description: Description of repair
            output: Repair output/suggestions
            
        Returns:
            Step object representing the repair
        """
        tokens_used = output.get('tokens_used', 0)

        # Use the most recent *attribution* step. v1 read run.steps[-1], which is
        # the routing step in the real pipeline order and has attribution=None,
        # so building this step raised AttributeError.
        attribution_step = self._get_attribution_step(run)

        step = Step(
            type=StepType.REPAIR,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS if success else StepStatus.FAILED,
            input={
                'attribution': attribution_step.attribution.to_dict() if attribution_step else {},
                'diff_size': len(run.diff),
                'model_provider': output.get('model_provider', 'unknown'),
                'context_bucket': output.get('context_bucket'),
                'source': output.get('source', 'generation'),
            },
            output={
                'repair_description': description,
                'repair_output': output,
                'success': success,
            },
            cost=Cost(tokens_used=int(tokens_used or 0), wall_clock_ms=0 if output.get('source') == 'fix_cache' else 2000)
        )
        return step

    def add_repair_to_run(self, run: Run, budget_guard=None, context_bucket=None, attempt_number: int = 1) -> bool:
        """
        Attempt repair and add step to run.

        Args:
            run: The Run to repair
            budget_guard: Optional BudgetGuard for retry tracking
            context_bucket: Optional v2 ContextBucket for this attempt
            attempt_number: 1-based semantic attempt number

        Returns:
            Whether repair was successful
        """
        # Record semantic retry attempt if budget guard provided
        if budget_guard:
            retry_success, retry_msg = budget_guard.record_retry(run.id, "semantic")
            if not retry_success:
                return False  # Semantic retry budget exhausted

        success, description, output = self.handle(
            run, context_bucket=context_bucket, attempt_number=attempt_number
        )
        if context_bucket is not None:
            output = dict(output)
            output['context_bucket'] = context_bucket.summary()
        step = self.create_repair_step(run, success, description, output)
        run.add_step(step)
        return success

    def add_cached_repair_to_run(self, run: Run, entry, lookup_path: str) -> Step:
        """
        Record a fix-cache reuse as a repair step. No model call, no retry charged.

        The step carries the cached patch so the Output Guardrail, Confidence
        Gate, Scope Guard and Verifier treat it exactly like a generated one.

        Args:
            run: The Run being repaired
            entry: FixCacheEntry being reused
            lookup_path: "signature" or "attribution" (which lookup found it)

        Returns:
            The appended Step
        """
        output = {
            'description': f"Reused verified fix {entry.fingerprint[:12]} from fix-cache ({lookup_path} match)",
            'reasoning': entry.attribution.get('claimed_cause', ''),
            'model_provider': 'fix_cache',
            'tokens_used': 0,
            'cost': 0.0,
            'fix_patch': entry.fix_patch,
            'source': 'fix_cache',
            'cache_fingerprint': entry.fingerprint,
            'cache_lookup_path': lookup_path,
            'cache_hit_count': entry.hit_count,
        }
        step = self.create_repair_step(run, True, output['description'], output)
        run.add_step(step)
        return step


def extract_section(content: str, label: str, stop_labels) -> str:
    """
    Pull a `LABEL:` section out of a model response, up to the next stop label,
    and strip a surrounding ``` code fence.
    
    Falls back to the whole response when the label is missing, so a patch is
    never silently empty just because the model ignored the format (if it is
    not a real diff, the Verifier's `git apply` rejects it).
    """
    if not content:
        return ""
    stops = "|".join(re.escape(s) for s in stop_labels)
    pattern = re.compile(
        rf"{re.escape(label)}:\s*(.*?)(?=^\s*(?:{stops})\s*:|\Z)", re.DOTALL | re.MULTILINE
    )
    match = pattern.search(content)
    body = match.group(1).strip() if match and match.group(1).strip() else content.strip()
    fence = re.match(r"^```[\w+-]*\s*\n(.*?)\n?```\s*$", body, re.DOTALL)
    return fence.group(1).strip("\n") if fence else body


def extract_fix_patch(content: str) -> str:
    """The PROPOSED_FIX section of a semantic repair response (fence stripped)."""
    return extract_section(content, "PROPOSED_FIX", ("WHY_THIS_WORKS", "RISK_ASSESSMENT", "ROOT_CAUSE"))
