"""Action Layer: Opens PRs and posts escalations"""

from typing import Dict, Any, Tuple, Optional
from enum import Enum
from src.models import Run, Step, StepType, StepLayer, StepStatus


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
    
    def __init__(self):
        """Initialize action layer"""
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
        Apply the fix by opening a PR.
        
        Args:
            run: The Run with approved fix
            
        Returns:
            Tuple of (success, pr_url_or_reason)
        """
        should_apply, reason = self.should_apply_fix(run)
        
        if not should_apply:
            return False, reason
        
        # In real system, this would:
        # 1. Create a branch
        # 2. Apply the patch
        # 3. Create a PR via GitHub/GitLab API
        # 4. Link to this Run's audit log
        
        # Simulate PR creation
        pr_url = f"https://github.com/{run.repo}/pull/12345"
        
        action = {
            'type': ActionType.OPEN_PR.value,
            'run_id': run.id,
            'pr_url': pr_url,
            'commit': run.failing_commit,
            'status': 'created',
        }
        self.actions_taken.append(action)
        
        return True, pr_url
    
    def escalate(self, run: Run, escalation_reason: str) -> Tuple[bool, str]:
        """
        Post an escalation message for human review.
        
        Args:
            run: The Run that needs escalation
            escalation_reason: Reason for escalation
            
        Returns:
            Tuple of (success, escalation_url_or_reason)
        """
        # Determine why we're escalating
        full_reason = self._build_escalation_reason(run, escalation_reason)
        
        # In real system, this would:
        # 1. Create an issue on GitHub
        # 2. Post to a Slack channel
        # 3. Send email notification
        # 4. Include full audit trail
        
        # Simulate escalation
        issue_url = f"https://github.com/{run.repo}/issues/54321"
        
        action = {
            'type': ActionType.POST_ESCALATION.value,
            'run_id': run.id,
            'issue_url': issue_url,
            'reason': full_reason,
            'status': 'posted',
        }
        self.actions_taken.append(action)
        
        return True, issue_url
    
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
        reasons.append("**Full audit log available in Run object**")
        
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
