"""Action Layer: Opens PRs and posts escalations"""

import base64
import os
from typing import Dict, Any, Tuple, Optional
from enum import Enum
from src.models import Run, Step, StepType, StepLayer, StepStatus
from src.github_client import GitHubClient, GitHubError
from src.sandbox import RepoSandbox, SandboxError


class ActionType(str, Enum):
    """Types of actions to take"""
    OPEN_PR = "open_pr"
    POST_ESCALATION = "post_escalation"
    NOTIFY = "notify"


class ActionLayer:
    """
    The ONLY component allowed to touch the real repository.
    
    Responsibilities:
    - Open PRs only after verification passes AND Scope Guard clears
    - Post escalation messages when fixes can't be auto-applied
    - Send notifications about status
    
    All actions are recorded in audit log before execution.
    """
    
    def __init__(self, github: Optional[GitHubClient] = None, sandbox: Optional[RepoSandbox] = None):
        """
        Initialize action layer.
        
        Args:
            github: GitHub REST client (default: from GITHUB_TOKEN / GITHUB_API_URL)
            sandbox: RepoSandbox used to re-apply the verified patch for the PR
        """
        self.github = github or GitHubClient()
        self.sandbox = sandbox or RepoSandbox()
        self.actions_taken: list = []
        self.kill_switch_enabled = False  # Can be set externally
    
    def should_apply_fix(self, run: Run) -> Tuple[bool, str]:
        """
        Determine if fix should be automatically applied.
        
        Checks multiple conditions:
        1. Kill switch is not engaged
        2. Verification passed
        3. Confidence gate approved
        4. Scope guard approved
        
        Args:
            run: The Run to evaluate
            
        Returns:
            Tuple of (should_apply, reason)
        """
        # Check 1: Kill switch
        if self.kill_switch_enabled:
            return False, "Kill switch is engaged - no auto-apply"
        
        # Check 2: Verification passed
        verification_step = self._get_verification_step(run)
        if not verification_step or not verification_step.output.get('passed'):
            return False, "Verification did not pass"
        
        # Check 3: Confidence gate approved
        confidence_step = self._get_confidence_gate_step(run)
        if not confidence_step or not confidence_step.output.get('auto_apply_eligible'):
            return False, "Confidence gate did not approve auto-apply"
        
        # Check 4: Scope guard approved
        scope_step = self._get_scope_guard_step(run)
        if not scope_step or not scope_step.output.get('in_scope'):
            return False, "Scope guard rejected patch (out of scope)"
        
        return True, "All checks passed - ready for auto-apply"
    
    def apply_fix(self, run: Run) -> Tuple[bool, str]:
        """
        Open a real pull request with the verified patch.
        
        Steps (GitHub REST, Git Data API):
          1. should_apply_fix() must pass (kill switch, verification, gate, scope).
          2. Re-apply the verified patch in a sandbox worktree at the failing
             commit and collect the changed files (src/sandbox.py).
          3. Create blobs -> tree (on the failing commit's tree) -> commit
             (parent = failing commit) -> branch `kintsugi/fix-<run id>`.
          4. Open a PR from that branch into the Run's branch
             (run.metadata['branch'], else KINTSUGI_DEFAULT_BASE_BRANCH, else "main").
        
        Nothing is pushed with local git credentials; only GITHUB_TOKEN is used.
        
        Args:
            run: The Run with approved fix
            
        Returns:
            Tuple of (success, pr_url_or_reason)
        """
        should_apply, reason = self.should_apply_fix(run)
        if not should_apply:
            return False, reason
        if not self.github.configured:
            self._record(ActionType.OPEN_PR, run, status='not_opened', reason='GITHUB_TOKEN not set')
            return False, "GITHUB_TOKEN not set; PR not opened"
        
        patch = self._get_verified_patch(run)
        if not patch:
            return False, "No patch found on the Run's repair step"
        
        try:
            head_sha, changes = self.sandbox.collect_patched_files(run.repo, run.failing_commit, patch)
            if not changes:
                return False, "Patch produced no file changes"
            repo_path = f"/repos/{run.repo}"
            base_tree = self.github.request("GET", f"{repo_path}/git/commits/{head_sha}")["tree"]["sha"]
            entries = []
            for change in changes:
                if change.content is None:
                    entries.append({'path': change.path, 'mode': change.mode, 'type': 'blob', 'sha': None})
                    continue
                blob = self.github.request("POST", f"{repo_path}/git/blobs", {
                    'content': base64.b64encode(change.content).decode('ascii'),
                    'encoding': 'base64',
                })
                entries.append({'path': change.path, 'mode': change.mode, 'type': 'blob', 'sha': blob['sha']})
            tree = self.github.request("POST", f"{repo_path}/git/trees", {'base_tree': base_tree, 'tree': entries})
            commit = self.github.request("POST", f"{repo_path}/git/commits", {
                'message': self._commit_message(run),
                'tree': tree['sha'],
                'parents': [head_sha],
            })
            branch = f"kintsugi/fix-{run.id[:8]}"
            self.github.request("POST", f"{repo_path}/git/refs", {'ref': f"refs/heads/{branch}", 'sha': commit['sha']})
            base_branch = (run.metadata or {}).get('branch') or os.getenv("KINTSUGI_DEFAULT_BASE_BRANCH") or "main"
            pr = self.github.request("POST", f"{repo_path}/pulls", {
                'title': f"Kintsugi: fix for failing commit {run.failing_commit[:8]}",
                'head': branch,
                'base': base_branch,
                'body': self._build_pr_body(run),
            })
        except (GitHubError, SandboxError) as exc:
            self._record(ActionType.OPEN_PR, run, status='failed', reason=str(exc))
            return False, str(exc)
        
        pr_url = pr.get('html_url', '')
        self._record(ActionType.OPEN_PR, run, status='created', pr_url=pr_url, branch=branch,
                     commit=run.failing_commit, fix_commit=commit['sha'])
        return True, pr_url
    
    def escalate(self, run: Run, escalation_reason: str) -> Tuple[bool, str]:
        """
        Open a GitHub issue with the full trace for human review.
        
        Without GITHUB_TOKEN nothing is posted: the escalation is recorded in
        actions_taken (and by the pipeline in the audit log) and this returns
        (False, reason). It never returns a made-up URL.
        
        Args:
            run: The Run that needs escalation
            escalation_reason: Reason for escalation
            
        Returns:
            Tuple of (success, issue_url_or_reason)
        """
        full_reason = self._build_escalation_reason(run, escalation_reason)
        if not self.github.configured:
            self._record(ActionType.POST_ESCALATION, run, status='not_posted',
                         reason=full_reason, detail='GITHUB_TOKEN not set')
            return False, "GITHUB_TOKEN not set; escalation recorded locally only"
        try:
            issue = self.github.request("POST", f"/repos/{run.repo}/issues", {
                'title': f"Kintsugi escalation: {run.failing_commit[:8] or run.id[:8]}",
                'body': full_reason,
            })
        except GitHubError as exc:
            self._record(ActionType.POST_ESCALATION, run, status='failed', reason=full_reason, detail=str(exc))
            return False, str(exc)
        issue_url = issue.get('html_url', '')
        self._record(ActionType.POST_ESCALATION, run, status='posted', issue_url=issue_url, reason=full_reason)
        return True, issue_url
    
    def _record(self, action_type: ActionType, run: Run, **details: Any) -> None:
        self.actions_taken.append({'type': action_type.value, 'run_id': run.id, **details})
    
    @staticmethod
    def _get_verified_patch(run: Run) -> Optional[str]:
        for step in reversed(run.steps):
            if step.type == StepType.REPAIR:
                return (step.output.get('repair_output') or {}).get('fix_patch')
        return None
    
    @staticmethod
    def _commit_message(run: Run) -> str:
        cause = next((s.attribution.claimed_cause for s in reversed(run.steps)
                      if s.type == StepType.ATTRIBUTION and s.attribution), "automated repair")
        return f"Kintsugi fix: {cause[:60]}\n\nRun {run.id}, failing commit {run.failing_commit}."
    
    def _build_pr_body(self, run: Run) -> str:
        lines = [f"Automated fix proposed by Kintsugi for failing commit `{run.failing_commit}`.", ""]
        for step in reversed(run.steps):
            if step.type == StepType.ATTRIBUTION and step.attribution:
                a = step.attribution
                lines += ["**Diagnosis**", f"- Cause: {a.claimed_cause}",
                          f"- Counterfactual (cause reverted, failing tests re-run): {a.counterfactual_result}"]
                lines += [f"- Evidence: {e}" for e in a.evidence_for[:3]]
                lines.append("")
                break
        for step in reversed(run.steps):
            if step.type == StepType.VERIFICATION and 'passed' in step.output:
                r = step.output.get('test_results', {})
                lines += ["**Verification**", f"- {step.output.get('reason')}",
                          f"- Command: `{r.get('test_command', '')}`",
                          f"- Passed: {r.get('tests_passed', 0)}, failed: {r.get('tests_failed', 0)}", ""]
                break
        lines.append(f"Kintsugi run id: `{run.id}`. Review before merging; nothing was merged automatically.")
        return "\n".join(lines)
    
    def _build_escalation_reason(self, run: Run, immediate_reason: str) -> str:
        """
        Build a detailed escalation reason with full context.
        
        Args:
            run: The Run being escalated
            immediate_reason: The immediate reason for escalation
            
        Returns:
            Detailed escalation reason
        """
        reasons = [f"**Escalation for {run.repo}**"]
        reasons.append(f"Failing commit: {run.failing_commit}")
        reasons.append("")
        reasons.append(f"**Reason for escalation:**")
        reasons.append(immediate_reason)
        reasons.append("")
        
        # Add relevant step information
        reasons.append("**Failure analysis:**")
        
        # Classification
        for step in run.steps:
            if step.type == StepType.ATTRIBUTION and 'classified_layer' in step.output:
                reasons.append(f"- Layer: {step.output['classified_layer']}")
                reasons.append(f"- Reason: {step.output.get('reason', 'N/A')}")
                break
        
        # Attribution
        for step in run.steps:
            if step.type == StepType.ATTRIBUTION and step.attribution:
                reasons.append(f"- Suspected cause: {step.attribution.claimed_cause}")
                if step.attribution.evidence_for:
                    reasons.append(f"- Supporting evidence: {'; '.join(step.attribution.evidence_for[:2])}")
                break
        
        reasons.append("")
        for step in reversed(run.steps):
            if step.type == StepType.VERIFICATION and 'passed' in step.output:
                reasons.append(f"- Last verification: {step.output.get('reason')}")
                tail = (step.output.get('test_results') or {}).get('output_tail')
                if tail:
                    reasons.append("")
                    reasons.append("<details><summary>Test output (tail)</summary>")
                    reasons.append("")
                    reasons.append("```")
                    reasons.append(tail[-2000:])
                    reasons.append("```")
                    reasons.append("</details>")
                break
        
        reasons.append("")
        reasons.append(f"Kintsugi run id: `{run.id}` (full trace in the audit log)")
        
        return "\n".join(reasons)
    
    def set_kill_switch(self, enabled: bool) -> None:
        """
        Enable/disable the kill switch.
        
        When enabled, no automatic fixes are applied.
        
        Args:
            enabled: Whether to enable the kill switch
        """
        self.kill_switch_enabled = enabled
    
    def get_kill_switch_status(self) -> bool:
        """
        Check if kill switch is engaged.
        
        Returns:
            Whether kill switch is enabled
        """
        return self.kill_switch_enabled
    
    def _get_verification_step(self, run: Run) -> Optional[Step]:
        """Get the verification step - looks for 'passed' key in output"""
        for step in reversed(run.steps):
            if step.type == StepType.VERIFICATION and 'passed' in step.output:
                return step
        return None
    
    def _get_confidence_gate_step(self, run: Run) -> Optional[Step]:
        """Get the confidence gate decision step - looks for 'auto_apply_eligible' key"""
        for step in reversed(run.steps):
            if step.type == StepType.VERIFICATION and 'auto_apply_eligible' in step.output:
                return step
        return None
    
    def _get_scope_guard_step(self, run: Run) -> Optional[Step]:
        """Get the scope guard decision step - looks for 'in_scope' key"""
        for step in reversed(run.steps):
            if step.type == StepType.VERIFICATION and 'in_scope' in step.output:
                return step
        return None
    
    def create_action_step(self, run: Run, action_type: ActionType, details: Dict[str, Any]) -> Step:
        """
        Create an action step to record what was done.
        
        Args:
            run: The Run the action was taken on
            action_type: Type of action
            details: Details about the action
            
        Returns:
            Step object representing the action
        """
        step = Step(
            type=StepType.VERIFICATION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            input={'action_type': action_type.value},
            output=details,
        )
        return step
    
    def get_actions_taken(self) -> list:
        """
        Get list of all actions taken.
        
        Returns:
            List of action records
        """
        return self.actions_taken.copy()
