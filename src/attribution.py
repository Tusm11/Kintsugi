"""Attribution Engine: Determines root cause for semantic failures using counterfactual reasoning"""

import hashlib
import keyword
import re
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from src.models import Run, Step, StepType, StepLayer, StepStatus, Attribution
from src.sandbox import RepoSandbox, failing_test_ids


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
    
    def __init__(self, sandbox: Optional[RepoSandbox] = None):
        """
        Initialize attribution engine.
        
        Args:
            sandbox: RepoSandbox used to execute counterfactuals (default: from env)
        """
        self.sandbox = sandbox or RepoSandbox()
        self.counterfactual_cache: Dict[str, Tuple[CounterfactualResult, str]] = {}
    
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
    
    def _test_counterfactual(
        self, cause: str, diff: str, run: Run, evidence: Optional[List[str]] = None
    ) -> Tuple[CounterfactualResult, str]:
        """
        Test the counterfactual for real: undo the suspected cause and re-run the failing tests.
        
        1. Pick the diff hunks that implement the suspected cause
           (`select_cause_hunks`: hunks whose file, or whose changed code's
           identifiers, are named in the cause/evidence; or the only hunk).
        2. In a sandbox worktree at the failing commit, reverse-apply just
           those hunks (`git apply -R`).
        3. Run the tests that failed (node ids parsed from the logs; the full
           suite if none can be parsed).
        
        Outcome: "pass" = undoing the cause makes the failing tests pass (cause
        confirmed); "fail" = they still fail; "inconclusive" = the cause could
        not be isolated to specific hunks, the hunks did not reverse-apply, or
        no local checkout is configured for the repo.
        
        No model is involved, so this step never affects attribution_source.
        
        Args:
            cause: The suspected cause
            diff: Unified diff of the failing commit
            run: The run being analyzed
            evidence: Supporting evidence lines (help locate the hunks)
            
        Returns:
            Tuple of (CounterfactualResult, source) where source is "execution"
            when tests actually ran, else "not_executed".
        """
        hunks_patch, selection_note = select_cause_hunks(cause, evidence or [], diff)
        if hunks_patch is None:
            return CounterfactualResult(cause, "fail", "inconclusive", f"Not executed: {selection_note}"), "not_executed"
        if not self.sandbox.is_configured(run.repo):
            return CounterfactualResult(
                cause, "fail", "inconclusive", f"Not executed: {self.sandbox.not_configured_reason(run.repo)}"
            ), "not_executed"
        
        cache_key = f"{run.repo}:{run.failing_commit}:{hashlib.sha256(hunks_patch.encode()).hexdigest()}"
        if cache_key in self.counterfactual_cache:
            return self.counterfactual_cache[cache_key]
        
        test_ids = failing_test_ids(run.failure_logs)
        result = self.sandbox.run_with_patch(run.repo, run.failing_commit, hunks_patch, reverse=True, test_ids=test_ids)
        scope = f"{len(test_ids)} failing test(s)" if test_ids else "full suite (no test ids in logs)"
        if not result.applied:
            outcome = CounterfactualResult(
                cause, "fail", "inconclusive",
                f"Suspected-cause hunks did not reverse-apply: {result.apply_error}"
            ), "not_executed"
        elif result.tests.timed_out:
            outcome = CounterfactualResult(cause, "fail", "inconclusive", f"Tests timed out ({scope})"), "execution"
        else:
            verdict = "pass" if result.passed else "fail"
            outcome = CounterfactualResult(
                cause, "fail", verdict,
                f"Reverted {selection_note}; ran {scope}; exit {result.tests.returncode}"
            ), "execution"
        self.counterfactual_cache[cache_key] = outcome
        return outcome
    
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
        
        # Counterfactual: executed (revert suspected hunks, re-run failing tests).
        # Not model reasoning, so it does not feed attribution_source.
        counterfactual_result, _counterfactual_source = self._test_counterfactual(
            claimed_cause, diff, run, evidence=evidence_for
        )
        
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


# ---------------------------------------------------------------------------
# Hunk selection for the executed counterfactual
# ---------------------------------------------------------------------------

_FILE_HDR_RE = re.compile(r"^--- (?:a/)?(\S+)")
_NEW_HDR_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")
_CODE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Keywords and ubiquitous names say nothing about *which* hunk a cause refers to.
_NOT_DISTINCTIVE = frozenset(keyword.kwlist) | {"self", "cls", "None", "True", "False", "print", "len", "str", "int"}


def _split_diff(diff: str) -> List[Dict[str, Any]]:
    """Unified diff -> [{'path', 'header': [lines], 'hunks': [[lines], ...]}]. Needs @@ headers."""
    files: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    lines = (diff or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _FILE_HDR_RE.match(line)
            new = _NEW_HDR_RE.match(lines[i + 1])
            path = (new.group(1) if new and new.group(1) != "/dev/null" else (old.group(1) if old else ""))
            current = {'path': path, 'header': [line, lines[i + 1]], 'hunks': []}
            files.append(current)
            i += 2
            continue
        if line.startswith("@@") and current is not None:
            current['hunks'].append([line])
        elif current is not None and current['hunks'] and (line[:1] in (" ", "+", "-", "\\") or line == ""):
            current['hunks'][-1].append(line)
        i += 1
    return [f for f in files if f['hunks']]


def select_cause_hunks(cause: str, evidence: List[str], diff: str) -> Tuple[Optional[str], str]:
    """
    Pick the hunks of `diff` that implement `cause`, as a patch to reverse-apply.
    
    Deterministic, no model:
      * a hunk is selected if its file path or basename is mentioned in the
        cause/evidence, or if an identifier (>= 3 chars) from its changed
        lines appears in the cause/evidence as a whole word;
      * if nothing matches and the diff has exactly one hunk, that hunk is it;
      * otherwise returns (None, reason): reverting the whole commit would only
        show the *commit* is responsible, not this specific cause.
    
    Returns:
        (patch_text or None, human-readable note on what was selected / why not)
    """
    files = _split_diff(diff)
    total = sum(len(f['hunks']) for f in files)
    if total == 0:
        return None, "diff has no hunks with @@ headers to revert"
    text = " ".join([cause or ""] + list(evidence or []))
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    
    selected: List[Tuple[Dict[str, Any], List[str]]] = []
    for f in files:
        path = f['path']
        base = path.rsplit("/", 1)[-1]
        file_named = bool(path) and (path in text or base in text)
        for hunk in f['hunks']:
            changed = " ".join(l[1:] for l in hunk[1:] if l[:1] in "+-")
            idents = {t for t in _CODE_IDENT_RE.findall(changed) if len(t) >= 3 and t not in _NOT_DISTINCTIVE}
            if file_named or (idents & words):
                selected.append((f, hunk))
    if not selected and total == 1:
        selected = [(files[0], files[0]['hunks'][0])]
        note = "the diff's only hunk"
    elif not selected:
        return None, f"could not isolate the suspected cause to specific hunks ({total} hunks, none referenced)"
    else:
        note = f"{len(selected)} of {total} hunk(s) matching the suspected cause"
    
    out: List[str] = []
    last_file = None
    for f, hunk in selected:
        if f is not last_file:
            out.extend(f['header'])
            last_file = f
        out.extend(hunk)
    return "\n".join(out) + "\n", note
