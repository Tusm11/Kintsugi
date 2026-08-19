"""Audit Log: Append-only, tamper-evident record of all decisions"""

from typing import Dict, Any, List
from datetime import datetime
from src.models import Run, Step


class AuditLog:
    """
    Append-only, tamper-evident record of every decision.
    
    Makes every guardrail's decision reviewable after the fact.
    Enables accountability and debugging.
    
    In production, this would be backed by a database or append-only log file
    with cryptographic hashing for tamper detection.
    """
    
    def __init__(self):
        """Initialize audit log"""
        self.entries: List[Dict[str, Any]] = []
        self.run_summaries: Dict[str, Dict[str, Any]] = {}
    
    def log_run_ingestion(self, run: Run) -> None:
        """
        Log when a run is ingested.
        
        Args:
            run: The Run being logged
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'run_ingestion',
            'run_id': run.id,
            'repo': run.repo,
            'commit': run.failing_commit,
            'metadata': run.metadata,
        }
        self.entries.append(entry)
    
    def log_classification(self, run: Run, layer: str, reason: str) -> None:
        """
        Log failure classification.
        
        Args:
            run: The Run being classified
            layer: The classified layer
            reason: Reason for classification
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'classification',
            'run_id': run.id,
            'layer': layer,
            'reason': reason,
        }
        self.entries.append(entry)
    
    def log_attribution(self, run: Run, claimed_cause: str, counterfactual: str) -> None:
        """
        Log attribution analysis.
        
        Args:
            run: The Run being analyzed
            claimed_cause: The suspected cause
            counterfactual: Result of counterfactual test
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'attribution',
            'run_id': run.id,
            'claimed_cause': claimed_cause,
            'counterfactual_result': counterfactual,
        }
        self.entries.append(entry)
    
    def log_routing_decision(self, run_id: str, handler: str, model_tier: str, reason: str) -> None:
        """
        Log routing decision.
        
        Args:
            run_id: The Run ID
            handler: Selected handler type
            model_tier: Selected model tier
            reason: Reason for routing
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'routing_decision',
            'run_id': run_id,
            'handler': handler,
            'model_tier': model_tier,
            'reason': reason,
        }
        self.entries.append(entry)
    
    def log_guardrail_check(self, run_id: str, guardrail_name: str, passed: bool, reason: str) -> None:
        """
        Log a guardrail check.
        
        Args:
            run_id: The Run ID
            guardrail_name: Name of the guardrail
            passed: Whether it passed
            reason: Reason/details
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'guardrail_check',
            'run_id': run_id,
            'guardrail': guardrail_name,
            'passed': passed,
            'reason': reason,
        }
        self.entries.append(entry)
    
    def log_scope_config_state(self, run_id: str, config: Any, allowed: bool) -> None:
        """
        Log the scope config state at the time of scope guard decision.
        
        Records the full config that was applied, including override flags and reasons.
        Six months later, someone reviewing an escalation trace can see whether
        allow_protected_paths_override was active at the time of decision.
        
        Args:
            run_id: The Run ID
            config: The ScopeConfig that was applied
            allowed: Whether the scope check passed
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'scope_config_state',
            'run_id': run_id,
            'max_files_touched': config.max_files_touched,
            'max_lines_changed': config.max_lines_changed,
            'allow_protected_paths_override': config.allow_protected_paths_override,
            'protected_paths_override_reason': config.protected_paths_override_reason if config.allow_protected_paths_override else None,
            'scope_check_allowed': allowed,
        }
        self.entries.append(entry)
    
    def log_repair_attempt(self, run_id: str, layer: str, success: bool, details: str) -> None:
        """
        Log a repair attempt.
        
        Args:
            run_id: The Run ID
            layer: Layer of repair (mechanical, structural, semantic)
            success: Whether repair was successful
            details: Details about the attempt
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'repair_attempt',
            'run_id': run_id,
            'layer': layer,
            'success': success,
            'details': details,
        }
        self.entries.append(entry)
    
    def log_verification(self, run_id: str, passed: bool, reason: str) -> None:
        """
        Log verification results.
        
        Args:
            run_id: The Run ID
            passed: Whether verification passed
            reason: Results summary
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'verification',
            'run_id': run_id,
            'passed': passed,
            'reason': reason,
        }
        self.entries.append(entry)
    
    def log_action(self, run_id: str, action_type: str, details: str, success: bool) -> None:
        """
        Log an action taken (PR opened, escalation posted).
        
        Args:
            run_id: The Run ID
            action_type: Type of action
            details: Details about the action
            success: Whether action succeeded
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'action',
            'run_id': run_id,
            'action_type': action_type,
            'details': details,
            'success': success,
        }
        self.entries.append(entry)
    
    def log_run_completion(self, run: Run, final_status: str) -> None:
        """
        Log completion of a run.
        
        Args:
            run: The completed Run
            final_status: Final status (healed, escalated, failed)
        """
        entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'event_type': 'run_completion',
            'run_id': run.id,
            'final_status': final_status,
            'total_steps': len(run.steps),
            'tokens_spent': run.spent.tokens_used,
            'time_spent_ms': run.spent.wall_clock_ms,
        }
        self.entries.append(entry)
        
        # Create summary for this run
        self.run_summaries[run.id] = {
            'repo': run.repo,
            'commit': run.failing_commit,
            'final_status': final_status,
            'ingestion_time': entry['timestamp'],
            'total_steps': len(run.steps),
            'steps_by_type': self._count_steps_by_type(run),
        }
    
    def _count_steps_by_type(self, run: Run) -> Dict[str, int]:
        """Count steps by type"""
        counts: Dict[str, int] = {}
        for step in run.steps:
            counts[step.type.value] = counts.get(step.type.value, 0) + 1
        return counts
    
    def get_run_log(self, run_id: str) -> List[Dict[str, Any]]:
        """
        Get all log entries for a specific run.
        
        Args:
            run_id: The Run ID
            
        Returns:
            List of log entries for that run
        """
        return [entry for entry in self.entries if entry.get('run_id') == run_id]
    
    def get_all_entries(self) -> List[Dict[str, Any]]:
        """
        Get all log entries.
        
        Returns:
            List of all audit log entries
        """
        return self.entries.copy()
    
    def get_run_summary(self, run_id: str) -> Dict[str, Any]:
        """
        Get summary for a run.
        
        Args:
            run_id: The Run ID
            
        Returns:
            Summary dictionary if run completed, None otherwise
        """
        return self.run_summaries.get(run_id)
    
    def get_all_summaries(self) -> Dict[str, Dict[str, Any]]:
        """
        Get summaries for all completed runs.
        
        Returns:
            Dictionary of run summaries
        """
        return self.run_summaries.copy()
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get overall statistics from the audit log.
        
        Returns:
            Dictionary with statistics
        """
        if not self.entries:
            return {'total_entries': 0, 'total_runs': 0}
        
        # Count events by type
        event_counts: Dict[str, int] = {}
        for entry in self.entries:
            event_type = entry.get('event_type', 'unknown')
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        
        # Count final statuses
        status_counts: Dict[str, int] = {}
        for summary in self.run_summaries.values():
            status = summary.get('final_status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1
        
        return {
            'total_entries': len(self.entries),
            'total_runs': len(self.run_summaries),
            'events_by_type': event_counts,
            'runs_by_status': status_counts,
            'first_entry_time': self.entries[0]['timestamp'] if self.entries else None,
            'last_entry_time': self.entries[-1]['timestamp'] if self.entries else None,
        }
