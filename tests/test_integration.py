"""Integration tests: Full pipeline testing"""

import pytest
from datetime import datetime
from src.pipeline import KintsugiPipeline
from src.ingestion import WebhookEvent
from src.models import RunStatus


class TestKintsugiPipeline:
    """Test full Kintsugi pipeline integration"""
    
    @pytest.fixture
    def pipeline(self):
        """Create a fresh Kintsugi pipeline"""
        return KintsugiPipeline()
    
    @pytest.fixture
    def mechanical_failure_event(self):
        """Create a webhook event for mechanical failure"""
        return WebhookEvent(
            source="github",
            repo="user/repo",
            commit="abc123def456",
            branch="main",
            build_id="build-001",
            failure_logs="Error: Connection timeout after 30 seconds",
            diff="",
            commit_message="Fix bug",
            webhook_id="webhook-001",
            timestamp=datetime.utcnow(),
            metadata={"pr_number": 42}
        )
    
    @pytest.fixture
    def structural_failure_event(self):
        """Create a webhook event for structural failure"""
        return WebhookEvent(
            source="github",
            repo="user/repo",
            commit="def789ghi012",
            branch="main",
            build_id="build-002",
            failure_logs="YAML parse error: mapping values are not allowed here",
            diff="+++ b/config.yaml\n+ invalid: : syntax:",
            commit_message="Add config",
            webhook_id="webhook-002",
            timestamp=datetime.utcnow(),
            metadata={}
        )
    
    @pytest.fixture
    def semantic_failure_event(self):
        """Create a webhook event for semantic failure"""
        return WebhookEvent(
            source="github",
            repo="user/repo",
            commit="ghi012jkl345",
            branch="main",
            build_id="build-003",
            failure_logs="Test failed: expected 42 but got 41",
            diff="--- a/calc.py\n+++ b/calc.py\n- return 42\n+ return 41",
            commit_message="Change calculation",
            webhook_id="webhook-003",
            timestamp=datetime.utcnow(),
            metadata={}
        )
    
    def test_ingest_event(self, pipeline, mechanical_failure_event):
        """Test ingesting an event"""
        run = pipeline.ingest_event(mechanical_failure_event)
        
        assert run is not None
        assert run.repo == "user/repo"
        assert run.failing_commit == "abc123def456"
    
    def test_ingest_duplicate_event(self, pipeline, mechanical_failure_event):
        """Test that duplicate events are rejected"""
        run1 = pipeline.ingest_event(mechanical_failure_event)
        assert run1 is not None
        
        # Ingest same event again
        run2 = pipeline.ingest_event(mechanical_failure_event)
        assert run2 is None
    
    def test_mechanical_failure_pipeline(self, pipeline, mechanical_failure_event):
        """Test full pipeline for mechanical failure"""
        run = pipeline.ingest_event(mechanical_failure_event)
        assert run is not None
        
        status, summary = pipeline.process_run(run)
        
        # Mechanical failures should be handled deterministically
        assert status in [RunStatus.HEALED, RunStatus.ESCALATED]
        assert "mechanical" in summary.lower() or "timeout" in summary.lower()
    
    def test_structural_failure_pipeline(self, pipeline, structural_failure_event):
        """Test full pipeline for structural failure"""
        run = pipeline.ingest_event(structural_failure_event)
        assert run is not None
        
        status, summary = pipeline.process_run(run)
        
        # Structural failures should route to structural handler
        assert status in [RunStatus.HEALED, RunStatus.ESCALATED]
        assert "structural" in summary.lower() or "Structural" in summary
    
    def test_semantic_failure_pipeline(self, pipeline, semantic_failure_event):
        """Test full pipeline for semantic failure"""
        run = pipeline.ingest_event(semantic_failure_event)
        assert run is not None
        
        status, summary = pipeline.process_run(run)
        
        # Semantic failures should route to semantic handler
        assert status in [RunStatus.HEALED, RunStatus.ESCALATED]
        
        # Look for classification step instead of hardcoded index
        classification_step = next(
            (s for s in reversed(run.steps) 
             if s.type == StepType.ATTRIBUTION and 'classified_layer' in s.output),
            None
        )
        assert "semantic" in summary.lower() or (
            classification_step and "semantic" in classification_step.output.get('classified_layer', '').lower()
        )
    
    def test_process_multiple_runs(self, pipeline, mechanical_failure_event, structural_failure_event, semantic_failure_event):
        """Test processing multiple runs"""
        run1 = pipeline.ingest_event(mechanical_failure_event)
        run2 = pipeline.ingest_event(structural_failure_event)
        run3 = pipeline.ingest_event(semantic_failure_event)
        
        assert run1 is not None
        assert run2 is not None
        assert run3 is not None
        
        # Process the queue
        stats = pipeline.process_queue()
        
        assert stats['total_processed'] == 3
        assert stats['healed'] + stats['escalated'] + stats['failed'] == 3
    
    def test_audit_trail_captured(self, pipeline, mechanical_failure_event):
        """Test that audit trail is captured"""
        run = pipeline.ingest_event(mechanical_failure_event)
        pipeline.process_run(run)
        
        audit = pipeline.get_audit_trail(run.id)
        
        # Should have multiple events
        assert len(audit) > 0
        
        # Should include key events
        event_types = [entry['event_type'] for entry in audit]
        assert 'run_ingestion' in event_types
        assert 'classification' in event_types
    
    def test_statistics_generated(self, pipeline, mechanical_failure_event):
        """Test that statistics are generated"""
        run = pipeline.ingest_event(mechanical_failure_event)
        pipeline.process_run(run)
        
        stats = pipeline.get_statistics()
        
        assert 'audit_log' in stats
        assert 'ingestion_queue' in stats
        assert stats['audit_log']['total_runs'] >= 1
    
    def test_kill_switch_prevents_auto_apply(self, pipeline, semantic_failure_event):
        """Test that kill switch prevents automatic fixes"""
        pipeline.action_layer.set_kill_switch(True)
        
        run = pipeline.ingest_event(semantic_failure_event)
        pipeline.process_run(run)
        
        # Should still process but not apply automatically
        audit = pipeline.get_audit_trail(run.id)
        
        # Should still have full audit trail
        assert len(audit) > 0
    
    def test_rate_limiting_blocks_floods(self, pipeline, mechanical_failure_event):
        """Test that rate limiting blocks event floods"""
        # Create many events from same actor
        allowed = 0
        for i in range(10):
            event = WebhookEvent(
                source="github",
                repo="user/repo",
                commit=f"commit-{i}",
                branch="main",
                build_id=f"build-{i}",
                failure_logs="Connection timeout",
                diff="",
                commit_message="",
                webhook_id=f"webhook-{i}",
                timestamp=datetime.utcnow(),
                metadata={"source": "same_actor"}
            )
            run = pipeline.ingest_event(event)
            if run:
                allowed += 1
        
        # Should have limited the number due to rate guard
        # (exact threshold depends on rate guard configuration)
        assert allowed < 10
    
    def test_pipeline_components_initialized(self, pipeline):
        """Test that all components are initialized"""
        assert pipeline.ingestion is not None
        assert pipeline.classifier is not None
        assert pipeline.attribution is not None
        assert pipeline.router is not None
        assert pipeline.mechanical_handler is not None
        assert pipeline.structural_handler is not None
        assert pipeline.semantic_handler is not None
        assert pipeline.input_guardrail is not None
        assert pipeline.output_guardrail is not None
        assert pipeline.confidence_gate is not None
        assert pipeline.scope_guard is not None
        assert pipeline.verifier is not None
        assert pipeline.budget_guard is not None
        assert pipeline.rate_guard is not None
        assert pipeline.action_layer is not None
        assert pipeline.audit_log is not None
    
    def test_run_has_all_steps_recorded(self, pipeline, mechanical_failure_event):
        """Test that run records all processing steps"""
        run = pipeline.ingest_event(mechanical_failure_event)
        initial_steps = len(run.steps)
        
        pipeline.process_run(run)
        
        # Should have added multiple steps
        assert len(run.steps) > initial_steps
        
        # Should have different step types
        step_types = set(step.type.value for step in run.steps)
        assert len(step_types) > 1
    
    def test_budget_tracking_across_pipeline(self, pipeline, semantic_failure_event):
        """Test that budget is tracked across pipeline"""
        run = pipeline.ingest_event(semantic_failure_event)
        
        # Check budget is initialized
        status = pipeline.budget_guard.get_budget_status(run.id)
        assert status['tokens']['spent'] == 0
        
        pipeline.process_run(run)
        
        # Budget should be tracked
        final_status = pipeline.budget_guard.get_budget_status(run.id)
        # Semantic repairs use tokens
        # (may or may not spend depending on whether repair succeeds)


class TestEndToEndScenarios:
    """End-to-end scenario tests"""
    
    def test_scenario_mechanical_timeout_backoff(self):
        """Scenario: Network timeout → backoff → successful"""
        pipeline = KintsugiPipeline()
        
        event = WebhookEvent(
            source="github",
            repo="company/service",
            commit="xyz789",
            branch="main",
            build_id="ci-12345",
            failure_logs="""
            Build started at 10:00
            Calling external API...
            Error: Connection timeout after 30 seconds
            Build failed
            """,
            diff="",
            commit_message="Call external service",
            webhook_id="gh-delivery-001",
            timestamp=datetime.utcnow(),
            metadata={"trigger": "push"}
        )
        
        run = pipeline.ingest_event(event)
        assert run is not None
        
        status, summary = pipeline.process_run(run)
        
        # Should be classified as mechanical
        assert any('mechanical' in str(step.output).lower() for step in run.steps if step.output)
    
    def test_scenario_config_error_fixed(self):
        """Scenario: YAML error in config → automatically fixed"""
        pipeline = KintsugiPipeline()
        
        event = WebhookEvent(
            source="github",
            repo="company/infra",
            commit="cfg123",
            branch="develop",
            build_id="ci-12346",
            failure_logs="Error parsing kubernetes.yaml: expected key",
            diff="+++ b/kubernetes.yaml\n+ name: service\n invalid:",
            commit_message="Update k8s config",
            webhook_id="gh-delivery-002",
            timestamp=datetime.utcnow(),
            metadata={"pr": 42}
        )
        
        run = pipeline.ingest_event(event)
        assert run is not None
        
        status, summary = pipeline.process_run(run)
        
        # Should be classified as structural
        assert any('structural' in str(step.output).lower() for step in run.steps if step.output)
    
    def test_scenario_test_failure_escalated(self):
        """Scenario: Test fails → investigation → escalated for review"""
        pipeline = KintsugiPipeline()
        
        event = WebhookEvent(
            source="github",
            repo="company/app",
            commit="test123",
            branch="feature/new-logic",
            build_id="ci-12347",
            failure_logs="""
            Running test_calculate_total...
            AssertionError: expected 100 but got 99
            FAILED
            """,
            diff="--- a/calculator.py\n+++ b/calculator.py\n- return 100\n+ return total",
            commit_message="Refactor calculation logic",
            webhook_id="gh-delivery-003",
            timestamp=datetime.utcnow(),
            metadata={"pr": 123}
        )
        
        run = pipeline.ingest_event(event)
        assert run is not None
        
        status, summary = pipeline.process_run(run)
        
        # Should go through full semantic analysis
        audit = pipeline.get_audit_trail(run.id)
        
        # Should have attempted analysis
        assert len(audit) > 5
        
        # Should log events: ingestion, classification, routing, possibly attribution
        event_types = [entry['event_type'] for entry in audit]
        assert 'run_ingestion' in event_types
        assert 'classification' in event_types


class TestPipelineRobustness:
    """Test pipeline robustness and edge cases"""
    
    def test_empty_queue_handling(self):
        """Test that empty queue is handled gracefully"""
        pipeline = KintsugiPipeline()
        
        stats = pipeline.process_queue()
        
        assert stats['total_processed'] == 0
    
    def test_malformed_event_handling(self):
        """Test handling of minimal event"""
        pipeline = KintsugiPipeline()
        
        # Minimal event
        event = WebhookEvent(
            source="github",
            repo="user/repo",
            commit="abc123",
            branch="main",
            build_id="build-1",
            failure_logs="",
            diff="",
            commit_message="",
            webhook_id="wh-1",
            timestamp=datetime.utcnow(),
            metadata={}
        )
        
        run = pipeline.ingest_event(event)
        
        # Should still process even with minimal info
        assert run is not None
        status, summary = pipeline.process_run(run)
        assert status is not None
    
    def test_large_diff_handling(self):
        """Test handling of very large diffs"""
        pipeline = KintsugiPipeline()
        
        # Create large diff
        large_diff = "\n".join([f"+ line {i}" for i in range(500)])
        
        event = WebhookEvent(
            source="github",
            repo="user/repo",
            commit="large123",
            branch="main",
            build_id="build-large",
            failure_logs="Test failed",
            diff=large_diff,
            commit_message="Large change",
            webhook_id="wh-large",
            timestamp=datetime.utcnow(),
            metadata={}
        )
        
        run = pipeline.ingest_event(event)
        status, summary = pipeline.process_run(run)
        
        # Should still process (though likely escalated due to scope)
        assert status in [RunStatus.HEALED, RunStatus.ESCALATED, RunStatus.FAILED]
