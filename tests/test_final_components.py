"""Tests for final components: Verifier, Budget, RateGuard, ActionLayer, AuditLog"""

import pytest
from datetime import datetime, timedelta
from src.verifier import Verifier
from src.budget import BudgetGuard
from src.rate_guard import RateAnomalyGuard
from src.action_layer import ActionLayer, ActionType
from src.audit_log import AuditLog
from src.models import Run, Step, StepType, StepLayer, Attribution, Budget


class TestVerifier:
    """Test Verifier component"""
    
    @pytest.fixture
    def verifier(self):
        """Create a fresh Verifier with no repo checkouts configured"""
        from src.sandbox import RepoSandbox
        return Verifier(sandbox=RepoSandbox(repo_paths={}))
    
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
    
    def test_verify_without_repair_step(self, verifier, base_run):
        """Test verification fails without repair step"""
        passed, reason, output = verifier.verify(base_run)
        
        assert passed is False
        assert "repair" in reason.lower()
    
    def test_verify_never_passes_without_a_checkout(self, verifier, base_run):
        """No local checkout configured: verification fails honestly (v1 simulated a pass here)"""
        attr = Attribution(claimed_cause="Logic error", counterfactual_result="pass")
        base_run.add_step(Step(type=StepType.ATTRIBUTION, attribution=attr))
        base_run.add_step(Step(type=StepType.REPAIR, layer=StepLayer.SEMANTIC,
                               output={'repair_output': {'fix_patch': '--- a/x\n+++ b/x\n'}}))
        
        passed, reason, output = verifier.verify(base_run)
        
        assert passed is False
        assert "No local checkout configured" in reason
    
    def test_create_verification_step(self, verifier, base_run):
        """Test creating verification step"""
        step = verifier.create_verification_step(
            base_run,
            True,
            "All tests passed",
            {'tests_run': 5, 'tests_passed': 5}
        )
        
        assert step.type == StepType.VERIFICATION
        assert step.output['passed'] is True
    
    def test_verify_and_record(self, verifier, base_run):
        """Test verify and record adds step to run"""
        repair_step = Step(type=StepType.REPAIR)
        base_run.add_step(repair_step)
        
        initial_steps = len(base_run.steps)
        verifier.verify_and_record(base_run)
        
        assert len(base_run.steps) == initial_steps + 1


class TestBudgetGuard:
    """Test Budget Guard"""
    
    @pytest.fixture
    def guard(self):
        """Create a fresh BudgetGuard"""
        return BudgetGuard()
    
    @pytest.fixture
    def run(self):
        """Create a test run"""
        return Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="",
            diff=""
            ,budget=Budget(max_retries_mechanical=3)
        )
    
    def test_initialize_budget(self, guard, run):
        """Test budget initialization"""
        guard.initialize_budget(run)
        
        status = guard.get_budget_status(run.id)
        assert status['tokens']['spent'] == 0
        assert status['tokens']['max'] == 10000
    
    def test_charge_tokens(self, guard, run):
        """Test charging tokens"""
        guard.initialize_budget(run)
        
        success, reason = guard.charge_tokens(run.id, 100)
        
        assert success is True
        assert guard.get_budget_status(run.id)['tokens']['spent'] == 100
    
    def test_charge_tokens_exceed_budget(self, guard, run):
        """Test charging tokens beyond budget"""
        guard.initialize_budget(run)
        
        success, _ = guard.charge_tokens(run.id, 10001)
        
        assert success is False
    
    def test_charge_time(self, guard, run):
        """Test charging wall-clock time"""
        guard.initialize_budget(run)
        
        success, _ = guard.charge_time(run.id, 5000)
        
        assert success is True
        assert guard.get_budget_status(run.id)['time_ms']['spent'] == 5000
    
    def test_record_retry(self, guard, run):
        """Test recording retry attempts"""
        guard.initialize_budget(run)
        
        success, _ = guard.record_retry(run.id, "mechanical")
        assert success is True
        
        success, _ = guard.record_retry(run.id, "mechanical")
        assert success is True
        
        success, _ = guard.record_retry(run.id, "mechanical")
        assert success is True
        
        # Fourth retry should fail
        success, _ = guard.record_retry(run.id, "mechanical")
        assert success is False
    
    def test_is_budget_exhausted(self, guard, run):
        """Test budget exhaustion detection"""
        guard.initialize_budget(run)
        
        # Spend tokens
        guard.charge_tokens(run.id, 10000)
        
        exhausted, reason = guard.is_budget_exhausted(run.id)
        
        assert exhausted is True
        assert "token" in reason.lower()
    
    def test_sync_run_budget(self, guard, run):
        """Test syncing budget to run"""
        guard.initialize_budget(run)
        guard.charge_tokens(run.id, 500)
        guard.charge_time(run.id, 2000)
        
        guard.sync_run_budget(run)
        
        assert run.spent.tokens_used == 500
        assert run.spent.wall_clock_ms == 2000


class TestRateAnomalyGuard:
    """Test Rate/Anomaly Guard"""
    
    @pytest.fixture
    def guard(self):
        """Create a fresh RateAnomalyGuard"""
        return RateAnomalyGuard(window_seconds=60, threshold_per_window=3)
    
    def test_record_event_allowed(self, guard):
        """Test recording allowed events"""
        allowed, reason = guard.record_event("repo1", "actor1")
        assert allowed is True
    
    def test_record_multiple_events(self, guard):
        """Test recording multiple events"""
        for i in range(3):
            allowed, _ = guard.record_event("repo1", "actor1")
            assert allowed is True
        
        # Fourth event should be rejected
        allowed, reason = guard.record_event("repo1", "actor1")
        assert allowed is False
        assert "rate limit" in reason.lower()
    
    def test_get_event_rate(self, guard):
        """Test getting event rate"""
        guard.record_event("repo1", "actor1")
        guard.record_event("repo1", "actor1")
        
        rate = guard.get_event_rate("repo1", "actor1")
        
        assert rate['events_in_window'] == 2
        assert rate['threshold'] == 3
    
    def test_is_actor_flagged(self, guard):
        """Test checking if actor is flagged"""
        for i in range(3):
            guard.record_event("repo1", "actor1")
        
        guard.record_event("repo1", "actor1")  # This flags the actor
        
        flagged = guard.is_actor_flagged("repo1", "actor1")
        assert flagged is True
    
    def test_unflag_actor(self, guard):
        """Test unflagging an actor"""
        # Flag the actor
        for i in range(4):
            guard.record_event("repo1", "actor1")
        
        assert guard.is_actor_flagged("repo1", "actor1")
        
        # Unflag
        success, reason = guard.unflag_actor("repo1", "actor1")
        
        assert success is True
        assert not guard.is_actor_flagged("repo1", "actor1")
    
    def test_detect_anomalies(self, guard):
        """Test anomaly detection"""
        # Create high volume
        for i in range(3):
            guard.record_event("repo1", "actor1")
        
        anomalies = guard.detect_anomalies("repo1")
        
        assert anomalies['repo'] == "repo1"
        assert len(anomalies['high_volume_actors']) > 0


class TestActionLayer:
    """Test Action Layer"""
    
    @pytest.fixture
    def action_layer(self):
        """Create a fresh ActionLayer with no GitHub token (real PR/issue tests live in test_sandbox.py)"""
        from src.github_client import GitHubClient
        return ActionLayer(github=GitHubClient(token=""))
    
    @pytest.fixture
    def approved_run(self):
        """Create a run that passed all checks"""
        run = Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="",
            diff="+++ b/fix.py\n+ return correct_value"
        )
        
        # Add verification step
        verification_step = Step(
            type=StepType.VERIFICATION,
            output={'passed': True, 'reason': 'All tests passed'}
        )
        run.add_step(verification_step)
        
        # Add confidence gate approval
        confidence_step = Step(
            type=StepType.VERIFICATION,
            output={'auto_apply_eligible': True, 'reasoning': 'All criteria met'}
        )
        run.add_step(confidence_step)
        
        # Add scope guard approval
        scope_step = Step(
            type=StepType.VERIFICATION,
            output={'in_scope': True, 'reasoning': 'Within scope'}
        )
        run.add_step(scope_step)
        
        return run
    
    def test_should_apply_fix_approved(self, action_layer, approved_run):
        """Test that approved runs can apply fix"""
        should_apply, reason = action_layer.should_apply_fix(approved_run)
        
        assert should_apply is True
    
    def test_should_apply_fix_kill_switch(self, action_layer, approved_run):
        """Test that kill switch prevents application"""
        action_layer.set_kill_switch(True)
        
        should_apply, reason = action_layer.should_apply_fix(approved_run)
        
        assert should_apply is False
        assert "kill switch" in reason.lower()
    
    def test_apply_fix_without_token_does_not_fake_a_pr(self, action_layer, approved_run):
        """Without GITHUB_TOKEN no PR is opened and no made-up URL is returned"""
        success, message = action_layer.apply_fix(approved_run)
        
        assert success is False
        assert "GITHUB_TOKEN" in message
        assert "github.com" not in message
    
    def test_escalate_without_token_is_recorded_locally(self, action_layer):
        """Escalation without GITHUB_TOKEN is recorded, not faked"""
        run = Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="",
            diff=""
        )
        
        success, message = action_layer.escalate(run, "Verification failed")
        
        assert success is False
        assert "github.com" not in message
        record = action_layer.get_actions_taken()[-1]
        assert record['status'] == 'not_posted'
        assert "Verification failed" in record['reason']
    
    def test_set_kill_switch(self, action_layer):
        """Test kill switch control"""
        assert action_layer.get_kill_switch_status() is False
        
        action_layer.set_kill_switch(True)
        assert action_layer.get_kill_switch_status() is True
        
        action_layer.set_kill_switch(False)
        assert action_layer.get_kill_switch_status() is False
    
    def test_get_actions_taken(self, action_layer, approved_run):
        """Test retrieving actions taken"""
        action_layer.apply_fix(approved_run)
        
        actions = action_layer.get_actions_taken()
        
        assert len(actions) == 1
        assert actions[0]['type'] == 'open_pr'


class TestAuditLog:
    """Test Audit Log"""
    
    @pytest.fixture
    def log(self):
        """Create a fresh AuditLog"""
        return AuditLog()
    
    @pytest.fixture
    def run(self):
        """Create a test run"""
        return Run(
            repo="user/repo",
            failing_commit="abc123",
            failure_logs="Test failed",
            diff="",
            commit_message="Fix bug"
        )
    
    def test_log_run_ingestion(self, log, run):
        """Test logging run ingestion"""
        log.log_run_ingestion(run)
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        assert entries[0]['event_type'] == 'run_ingestion'
    
    def test_log_classification(self, log, run):
        """Test logging classification"""
        log.log_classification(run, "semantic", "Test assertion failed")
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        assert entries[0]['event_type'] == 'classification'
        assert entries[0]['layer'] == 'semantic'
    
    def test_log_attribution(self, log, run):
        """Test logging attribution"""
        log.log_attribution(run, "Missing return", "pass")
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        assert entries[0]['event_type'] == 'attribution'
    
    def test_log_multiple_events(self, log, run):
        """Test logging multiple events for same run"""
        log.log_run_ingestion(run)
        log.log_classification(run, "semantic", "Failed")
        log.log_attribution(run, "Bug", "pass")
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 3
    
    def test_log_run_completion(self, log, run):
        """Test logging run completion"""
        log.log_run_completion(run, "healed")
        
        summary = log.get_run_summary(run.id)
        assert summary is not None
        assert summary['final_status'] == 'healed'
    
    def test_get_statistics(self, log, run):
        """Test getting audit log statistics"""
        log.log_run_ingestion(run)
        log.log_classification(run, "semantic", "Failed")
        log.log_run_completion(run, "escalated")
        
        stats = log.get_statistics()
        assert stats['total_entries'] >= 3
        assert stats['total_runs'] >= 1
        assert 'events_by_type' in stats
    
    def test_log_guardrail_check(self, log, run):
        """Test logging guardrail check"""
        log.log_guardrail_check(run.id, "input_guardrail", True, "Content is safe")
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        assert entries[0]['event_type'] == 'guardrail_check'
    
    def test_log_repair_attempt(self, log, run):
        """Test logging repair attempt"""
        log.log_repair_attempt(run.id, "semantic", True, "Applied code fix")
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        assert entries[0]['event_type'] == 'repair_attempt'
    
    def test_log_verification(self, log, run):
        """Test logging verification"""
        log.log_verification(run.id, True, "All tests passed")
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        assert entries[0]['event_type'] == 'verification'
    
    def test_log_action(self, log, run):
        """Test logging action taken"""
        log.log_action(run.id, "open_pr", "PR #123 opened", True)
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        assert entries[0]['event_type'] == 'action'
    
    def test_log_scope_config_state(self, log):
        """Test logging scope config state"""
        from src.models import ScopeConfig
        
        run = Run(repo="user/repo", failing_commit="abc123", failure_logs="", diff="")
        config = ScopeConfig(
            max_files_touched=10,
            max_lines_changed=200,
            allow_protected_paths_override=True,
            protected_paths_override_reason="Approved by security team"
        )
        
        log.log_scope_config_state(run.id, config, allowed=True)
        
        entries = log.get_run_log(run.id)
        assert len(entries) == 1
        
        entry = entries[0]
        assert entry['event_type'] == 'scope_config_state'
        assert entry['max_files_touched'] == 10
        assert entry['max_lines_changed'] == 200
        assert entry['allow_protected_paths_override'] is True
        assert 'security team' in entry['protected_paths_override_reason']
        assert entry['scope_check_allowed'] is True
    
    def test_scope_config_state_shows_override_in_audit_trail(self, log):
        """Test that audit trail captures whether override was active."""
        from src.models import ScopeConfig
        
        run = Run(repo="user/repo", failing_commit="abc123", failure_logs="", diff="")
        config_without_override = ScopeConfig(allow_protected_paths_override=False)
        config_with_override = ScopeConfig(
            allow_protected_paths_override=True,
            protected_paths_override_reason="Approved for migration"
        )
        
        # Log run with override inactive
        log.log_scope_config_state(run.id, config_without_override, True)
        
        # Check audit trail
        entries = log.get_run_log(run.id)
        assert entries[0]['allow_protected_paths_override'] is False
        assert entries[0]['protected_paths_override_reason'] is None
        
        # Log another scenario with override active
        log.log_scope_config_state(run.id, config_with_override, True)
        
        entries = log.get_run_log(run.id)
        # Most recent entry
        latest = entries[-1]
        assert latest['allow_protected_paths_override'] is True
        assert 'migration' in latest['protected_paths_override_reason']
    
    def test_get_all_entries(self, log, run):
        """Test retrieving all entries"""
        log.log_run_ingestion(run)
        log.log_classification(run, "semantic", "Failed")
        
        all_entries = log.get_all_entries()
        assert len(all_entries) >= 2
    
    def test_get_all_summaries(self, log, run):
        """Test retrieving all summaries"""
        log.log_run_completion(run, "healed")
        
        summaries = log.get_all_summaries()
        assert run.id in summaries
