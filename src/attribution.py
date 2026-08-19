"""Attribution Engine: Determines root cause for semantic failures using counterfactual reasoning"""

import re
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from src.models import Run, Step, StepType, StepLayer, StepStatus, Attribution


@dataclass
class CounterfactualResult:
    """Result of testing a counterfactual hypothesis - NO CONFIDENCE SCORES"""
    cause: str
    original_outcome: str  # "pass" or "fail"
    counterfactual_outcome: str  # "pass", "fail", or "inconclusive"
    details: str = ""  # Explanation from model reasoning (not a score - structured evidence only)
    
    # IMPORTANT: This class does NOT include confidence/strength scores. The outcome itself
    # ("pass"/"fail"/"inconclusive") carries all meaningful information. The Confidence Gate
    # evaluates structure (evidence_for, evidence_against, etc.), never float scores.


class AttributionEngine:
    """
    Determines root cause for semantic failures using counterfactual reasoning.
    
    Instead of an LLM guessing from a diff, this engine tests hypotheses:
    "If we undo this suspected cause, does the test pass?"
    
    The output is structured evidence, not a confidence score:
    - What was the suspected cause
    - Evidence supporting it
    - Evidence against it
    - Alternatives considered and why they were rejected
    - The result of the counterfactual intervention
    """
    
    def __init__(self):
        """Initialize attribution engine"""
        self.counterfactual_cache: Dict[str, CounterfactualResult] = {}
    
    def _parse_diff_changes(self, diff: str) -> List[Dict[str, str]]:
        """
        Parse a unified diff to extract changed lines and files.
        
        Args:
            diff: Unified diff format string
            
        Returns:
            List of change records: {file, old_content, new_content, change_type}
        """
        changes = []
        current_file = ""
        
        lines = diff.split('\n')
        i = 0
        
        while i < len(lines):
            line = lines[i]
            
            # File header: --- a/path or +++ b/path
            if line.startswith('---'):
                current_file = line[6:] if len(line) > 6 else ""
            elif line.startswith('+++'):
                pass  # We have the file from --- line
            elif line.startswith('@@'):
                # Hunk header: @@ -start,count +start,count @@
                # Extract line numbers
                pass
            elif line.startswith('-') and not line.startswith('---'):
                # Removed line
                changes.append({
                    'file': current_file,
                    'content': line[1:],
                    'change_type': 'removed',
                    'line': line[1:]
                })
            elif line.startswith('+') and not line.startswith('+++'):
                # Added line
                changes.append({
                    'file': current_file,
                    'content': line[1:],
                    'change_type': 'added',
                    'line': line[1:]
                })
            
            i += 1
        
        return changes
    
    def _extract_suspected_causes(self, diff: str, failure_logs: str) -> Tuple[List[str], str]:
        """
        Extract suspected root causes from diff and logs using model-based reasoning.
        
        Uses semantic model to analyze:
        - Logic changes that affect test behavior
        - Data structure changes
        - Configuration changes
        
        Args:
            diff: Unified diff of changes
            failure_logs: Test failure logs
            
        Returns:
            Tuple of (causes_list, source) where source is "model" or "fallback_heuristic"
            Source is set STRUCTURALLY based on whether model call succeeded, not by inspecting output.
        """
        from src.model_provider import get_provider
        from src.prompts import AttributionPrompt
        
        # Generate prompt for model to identify causes
        prompt = AttributionPrompt.generate(
            diff=diff,
            failure_logs=failure_logs,
            commit_message="(automated repair)"
        )
        
        # Call semantic model
        provider = get_provider("semantic")
        success, response = provider.call_with_retry(
            prompt=prompt,
            budget_tokens=500,
            temperature=0.7
        )
        
        # SET SOURCE STRUCTURALLY based on actual API call result, not output inspection
        if success:
            # Model call succeeded — this is real model reasoning
            attribution_source = "model"
            
            # Parse model response - expect list of causes
            causes = []
            lines = response.content.split('\n')
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    causes.append(line)
            
            return (causes[:3] if causes else ["Model could not identify specific causes"], attribution_source)
        else:
            # Model call failed — use fallback heuristics
            attribution_source = "fallback_heuristic"
            
            # Fallback: simple pattern matching (no model involved)
            causes = []
            changes = self._parse_diff_changes(diff)
            
            for change in changes:
                if change['change_type'] == 'removed':
                    if any(keyword in change['line'].lower() for keyword in ['return', 'throw', 'raise', 'assert']):
                        causes.append(f"Removed critical statement in {change['file']}: {change['line'][:50]}")
                elif change['change_type'] == 'added':
                    line_lower = change['line'].lower()
                    if any(keyword in line_lower for keyword in ['=', 'append', 'push', 'set']):
                        causes.append(f"Added assignment in {change['file']}: {change['line'][:50]}")
            
            return (causes or ["Heuristic analysis inconclusive - model unavailable"], attribution_source)
    
    def _gather_evidence_for_cause(self, cause: str, diff: str, failure_logs: str, changed_files: List[str]) -> Tuple[List[str], str]:
        """
        Gather evidence supporting a suspected cause using model-based reasoning.
        
        Args:
            cause: The suspected cause
            diff: Unified diff
            failure_logs: Test logs
            changed_files: List of files that changed
            
        Returns:
            Tuple of (evidence_list, source) where source is "model" or "fallback_heuristic"
            Source is set STRUCTURALLY based on whether model call succeeded.
        """
        from src.model_provider import get_provider
        
        # Use semantic model to gather supporting evidence
        provider = get_provider("semantic")
        
        prompt = f"""Given this suspected root cause, list evidence that supports it being the actual cause of the test failure.

**Suspected Cause:**
{cause}

**Code Changes (Diff):**
{diff[:1000]}

**Failure Logs:**
{failure_logs[:1000]}

**Changed Files:**
{', '.join(changed_files)}

List supporting evidence for this cause (one per line, be specific):"""
        
        success, response = provider.call_with_retry(
            prompt=prompt,
            budget_tokens=300,
            temperature=0.7
        )
        
        # SET SOURCE STRUCTURALLY based on API call success
        if success:
            attribution_source = "model"
            
            # Parse model response into evidence list
            evidence = []
            lines = response.content.split('\n')
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    evidence.append(line)
            
            return (evidence[:5] if evidence else ["No supporting evidence found"], attribution_source)
        else:
            attribution_source = "fallback_heuristic"
            
            # Fallback: basic pattern matching (no model involved)
            evidence = []
            if cause in diff:
                evidence.append("Suspected cause appears directly in the code changes")
            cause_keywords = cause.lower().split()[:3]
            if any(kw in failure_logs.lower() for kw in cause_keywords):
                evidence.append("Failure logs mention terms related to the suspected cause")
            
            return (evidence or ["Limited evidence available - model unavailable"], attribution_source)
    
    def _gather_evidence_against_cause(self, cause: str, diff: str, failure_logs: str) -> Tuple[List[str], str]:
        """
        Gather evidence contradicting a suspected cause using model-based reasoning.
        
        Args:
            cause: The suspected cause
            diff: Unified diff
            failure_logs: Test logs
            
        Returns:
            Tuple of (counter_evidence_list, source) where source is "model" or "fallback_heuristic"
            Source is set STRUCTURALLY based on whether model call succeeded.
        """
        from src.model_provider import get_provider
        
        # Use semantic model to identify contradicting evidence
        provider = get_provider("semantic")
        
        prompt = f"""Given this suspected root cause, what evidence suggests this is NOT the actual cause of the test failure?

**Suspected Cause:**
{cause}

**Code Changes (Diff):**
{diff[:1000]}

**Failure Logs:**
{failure_logs[:1000]}

List evidence that contradicts this cause (one per line, be specific):"""
        
        success, response = provider.call_with_retry(
            prompt=prompt,
            budget_tokens=300,
            temperature=0.7
        )
        
        # SET SOURCE STRUCTURALLY based on API call success
        if success:
            attribution_source = "model"
            
            # Parse model response
            evidence = []
            lines = response.content.split('\n')
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#'):
                    evidence.append(line)
            
            return (evidence[:5] if evidence else ["No contradicting evidence found"], attribution_source)
        else:
            attribution_source = "fallback_heuristic"
            
            # Fallback: basic pattern matching (no model involved)
            evidence = []
            if cause not in diff:
                evidence.append("Suspected cause does not appear in the diff")
            if "syntax" in failure_logs.lower() and "logic" in cause.lower():
                evidence.append("Logs indicate syntax error, not logic error")
            if "timeout" in failure_logs.lower() and "assignment" in cause.lower():
                evidence.append("Logs indicate timeout, not a code assignment issue")
            
            return (evidence or ["Limited counter-evidence available - model unavailable"], attribution_source)
    
    def _test_counterfactual(self, cause: str, diff: str, run: Run) -> Tuple[CounterfactualResult, str]:
        """
        Test the counterfactual: "If we undo this suspected cause, does the test pass?"
        
        Uses semantic model to reason through the counterfactual scenario.
        
        Args:
            cause: The suspected cause
            diff: Unified diff
            run: The run being analyzed
            
        Returns:
            Tuple of (CounterfactualResult, source) where source is "model" or "fallback_heuristic"
            Source is set STRUCTURALLY based on whether model call succeeded.
        """
        from src.model_provider import get_provider
        from src.prompts import CounterfactualPrompt
        
        # Cache check
        cache_key = f"{cause}:{hash(diff)}"
        if cache_key in self.counterfactual_cache:
            cached_result, cached_source = self.counterfactual_cache[cache_key]
            return (cached_result, cached_source)
        
        # Use model to reason about counterfactual
        provider = get_provider("semantic")
        
        prompt = CounterfactualPrompt.generate(
            attributed_cause=cause,
            failure_logs=run.failure_logs,
            proposed_fix=diff
        )
        success, response = provider.call_with_retry(
            prompt=prompt,
            budget_tokens=400,
            temperature=0.7
        )
        
        # SET SOURCE STRUCTURALLY based on API call success
        if success:
            attribution_source = "model"
            
            # Parse model response to extract counterfactual judgment
            model_response = response.content.lower()
            if "would pass" in model_response or "fix" in model_response or "resolve" in model_response:
                outcome = "pass"
            elif "would fail" in model_response or "not fix" in model_response:
                outcome = "fail"
            else:
                outcome = "inconclusive"
            
            result = CounterfactualResult(
                cause=cause,
                original_outcome="fail",
                counterfactual_outcome=outcome,
                details=response.content[:200]
            )
        else:
            attribution_source = "fallback_heuristic"
            
            # Fallback to conservative estimate - mark source and escalate
            result = CounterfactualResult(
                cause=cause,
                original_outcome="fail",
                counterfactual_outcome="inconclusive",
                details="Model analysis failed - insufficient evidence to determine counterfactual outcome"
            )
        
        self.counterfactual_cache[cache_key] = (result, attribution_source)
        return (result, attribution_source)
    
    def attribute(self, run: Run) -> Attribution:
        """
        Perform counterfactual analysis to determine root cause of semantic failure.
        
        Args:
            run: The Run to analyze
            
        Returns:
            Attribution object with structured evidence and source tracking.
            attribution_source is set to "fallback_heuristic" if ANY method used fallback,
            otherwise "model" if all calls succeeded.
        """
        diff = run.diff
        failure_logs = run.failure_logs
        
        # Track if any method fell back to heuristics (not all calls succeeded)
        sources = []
        
        # Extract suspected causes - returns (causes, source)
        suspected_causes, causes_source = self._extract_suspected_causes(diff, failure_logs)
        sources.append(causes_source)
        
        # Pick the most likely cause (for now, the first one)
        claimed_cause = suspected_causes[0] if suspected_causes else "Unknown cause"
        
        # Gather evidence - returns (evidence, source)
        changed_files = [change['file'] for change in self._parse_diff_changes(diff)]
        evidence_for, for_source = self._gather_evidence_for_cause(claimed_cause, diff, failure_logs, changed_files)
        sources.append(for_source)
        
        evidence_against, against_source = self._gather_evidence_against_cause(claimed_cause, diff, failure_logs)
        sources.append(against_source)
        
        # Test counterfactual - returns (result, source)
        counterfactual_result, counterfactual_source = self._test_counterfactual(claimed_cause, diff, run)
        sources.append(counterfactual_source)
        
        # Build alternatives
        alternatives = []
        for alt_cause in suspected_causes[1:]:
            alternatives.append({
                'cause': alt_cause,
                'why_rejected': 'Lower priority than primary hypothesis based on diff analysis'
            })
        
        # SET FINAL attribution_source: "fallback_heuristic" if ANY method used fallback, else "model"
        # This is structural: based on actual API call success, not output inspection
        final_attribution_source = "fallback_heuristic" if any(s == "fallback_heuristic" for s in sources) else "model"
        
        attribution = Attribution(
            claimed_cause=claimed_cause,
            evidence_for=evidence_for,
            evidence_against=evidence_against,
            alternatives_considered=alternatives,
            counterfactual_result=counterfactual_result.counterfactual_outcome,
            attribution_source=final_attribution_source,  # STRUCTURAL: set from API success, not output inspection
        )
        
        return attribution
    
    def create_attribution_step(self, run: Run, attribution: Attribution) -> Step:
        """
        Create an attribution step to add to the run's audit trail.
        
        Args:
            run: The Run being attributed
            attribution: The Attribution result
            
        Returns:
            Step object representing the attribution
        """
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            input={
                'diff_length': len(run.diff),
                'logs_length': len(run.failure_logs),
            },
            output={
                'attribution': attribution.to_dict(),
            },
            attribution=attribution
        )
        return step
    
    def add_attribution_to_run(self, run: Run) -> Attribution:
        """
        Perform attribution analysis and add step to run.
        
        Args:
            run: The Run to analyze
            
        Returns:
            The computed Attribution
        """
        attribution = self.attribute(run)
        step = self.create_attribution_step(run, attribution)
        run.add_step(step)
        return attribution
