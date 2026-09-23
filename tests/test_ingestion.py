"""Tests for Ingestion Layer"""

import pytest
from datetime import datetime
from src.ingestion import IngestionLayer, WebhookEvent
from src.models import Run, StepType, StepLayer, StepStatus


class TestWebhookEvent:
    """Test WebhookEvent dataclass"""
    
    def test_webhook_event_creation(self):
        event = WebhookEvent(
            source="github",
            repo="user/repo",
            commit="abc123def456",
            branch="main",
            build_id="build-789",
            failure_logs="Test failed",
            diff="--- a/file.py\n+++ b/file.py",
            commit_message="Fix bug",
            webhook_id="webhook-123",
            timestamp=datetime.utcnow(),
            metadata={"pr_number": 42}
        )
        assert event.source == "github"
        assert event.repo == "user/repo"
        assert event.metadata['pr_number'] == 42


class TestIngestionLayer:
    """Test Ingestion Layer"""
    
    @pytest.fixture
    def ingestion(self):
        """Create a fresh IngestionLayer for each test"""
        return IngestionLayer()
    
    @pytest.fixture
    def sample_event(self):
        """Create a sample webhook event"""
        return WebhookEvent(
            source="github",
            repo="user/repo",
            commit="abc123",
            branch="main",
            build_id="build-001",
            failure_logs="Error: test failed",
            diff="--- file.py\n+++ file.py",
            commit_message="Add feature",
            webhook_id="webhook-001",
            timestamp=datetime.utcnow(),
            metadata={"pr": 10}
        )
    
    def test_initialization(self, ingestion):
        """Test ingestion layer initializes correctly"""
        assert ingestion.processed_count == 0
        assert ingestion.deduplicated_count == 0
        assert len(ingestion.runs_queue) == 0
        assert len(ingestion.processed_webhook_ids) == 0
    
    def test_ingest_new_event(self, ingestion, sample_event):
        """Test ingesting a new event creates a Run"""
        run = ingestion.ingest(sample_event)
        
        assert run is not None
        assert isinstance(run, Run)
        assert run.repo == "user/repo"
        assert run.failing_commit == "abc123"
        assert ingestion.processed_count == 1
        assert ingestion.deduplicated_count == 0
    
    def test_run_has_ingestion_step(self, ingestion, sample_event):
        """Test that created Run has an ingestion step"""
        run = ingestion.ingest(sample_event)
        
        assert len(run.steps) > 0
        ingestion_step = run.steps[0]
        assert ingestion_step.type == StepType.VERIFICATION
        assert ingestion_step.status == StepStatus.SUCCESS
        assert ingestion_step.output['run_id'] == run.id
    
    def test_run_metadata_from_event(self, ingestion, sample_event):
        """Test that Run captures event metadata"""
        run = ingestion.ingest(sample_event)
        
        assert run.metadata['source'] == "github"
        assert run.metadata['branch'] == "main"
        assert run.metadata['build_id'] == "build-001"
        assert run.metadata['webhook_id'] == "webhook-001"
        assert run.metadata['pr'] == 10
    
    def test_duplicate_webhook_rejected(self, ingestion, sample_event):
        """Test that duplicate webhook deliveries are rejected"""
        # First delivery
        run1 = ingestion.ingest(sample_event)
        assert run1 is not None
        assert ingestion.processed_count == 1
        
        # Second delivery of same webhook (retry)
        run2 = ingestion.ingest(sample_event)
        assert run2 is None
        assert ingestion.deduplicated_count == 1
        assert ingestion.processed_count == 1  # Still 1
    
    def test_different_webhook_ids_same_build_deduplicated(self, ingestion, sample_event):
        """Test that duplicate builds are detected even with different webhook IDs"""
        event1 = sample_event
        event2 = WebhookEvent(
            source="github",
            repo=sample_event.repo,
            commit=sample_event.commit,
            branch=sample_event.branch,
            build_id=sample_event.build_id,  # Same build
            failure_logs=sample_event.failure_logs,
            diff=sample_event.diff,
            commit_message=sample_event.commit_message,
            webhook_id="webhook-002",  # Different webhook ID
            timestamp=datetime.utcnow(),
            metadata={}
        )
        
        run1 = ingestion.ingest(event1)
        assert run1 is not None
        
        run2 = ingestion.ingest(event2)
        assert run2 is None
        assert ingestion.deduplicated_count == 1
    
    def test_different_builds_not_deduplicated(self, ingestion, sample_event):
        """Test that different builds are not deduplicated"""
        event1 = sample_event
        event2 = WebhookEvent(
            source="github",
            repo=sample_event.repo,
            commit=sample_event.commit,
            branch=sample_event.branch,
            build_id="build-002",  # Different build
            failure_logs=sample_event.failure_logs,
            diff=sample_event.diff,
            commit_message=sample_event.commit_message,
            webhook_id="webhook-002",
            timestamp=datetime.utcnow(),
            metadata={}
        )
        
        run1 = ingestion.ingest(event1)
        run2 = ingestion.ingest(event2)
        
        assert run1 is not None
        assert run2 is not None
        assert run1.id != run2.id
        assert ingestion.processed_count == 2
        assert ingestion.deduplicated_count == 0
    
    def test_get_pending_runs(self, ingestion, sample_event):
        """Test retrieving pending runs from queue"""
        event1 = sample_event
        event2 = WebhookEvent(
            source="github",
            repo="another/repo",
            commit="def789",
            branch="develop",
            build_id="build-002",
            failure_logs="Another error",
            diff="",
            commit_message="Another commit",
            webhook_id="webhook-002",
            timestamp=datetime.utcnow(),
            metadata={}
        )
        
        run1 = ingestion.ingest(event1)
        run2 = ingestion.ingest(event2)
        
        pending = ingestion.get_pending_runs()
        assert len(pending) == 2
        assert run1 in pending
        assert run2 in pending
    
    def test_dequeue_run(self, ingestion, sample_event):
        """Test dequeueing runs in FIFO order"""
        event1 = sample_event
        event2 = WebhookEvent(
            source="github",
            repo="another/repo",
            commit="def789",
            branch="develop",
            build_id="build-002",
            failure_logs="Another error",
            diff="",
            commit_message="Another commit",
            webhook_id="webhook-002",
            timestamp=datetime.utcnow(),
            metadata={}
        )
        
        run1 = ingestion.ingest(event1)
        run2 = ingestion.ingest(event2)
        
        # First dequeue should return first ingested
        dequeued1 = ingestion.dequeue_run()
        assert dequeued1.id == run1.id
        assert len(ingestion.get_pending_runs()) == 1
        
        # Second dequeue
        dequeued2 = ingestion.dequeue_run()
        assert dequeued2.id == run2.id
        assert len(ingestion.get_pending_runs()) == 0
        
        # Third dequeue should return None
        dequeued3 = ingestion.dequeue_run()
        assert dequeued3 is None
    
    def test_get_stats(self, ingestion, sample_event):
        """Test ingestion statistics"""
        event1 = sample_event
        event2 = WebhookEvent(
            source="github",
            repo="another/repo",
            commit="def789",
            branch="develop",
            build_id="build-002",
            failure_logs="Error",
            diff="",
            commit_message="Commit",
            webhook_id="webhook-002",
            timestamp=datetime.utcnow(),
            metadata={}
        )
        
        ingestion.ingest(event1)
        ingestion.ingest(event2)
        ingestion.ingest(event1)  # Duplicate
        
        stats = ingestion.get_stats()
        assert stats['processed_count'] == 2
        assert stats['deduplicated_count'] == 1
        assert stats['pending_runs'] == 2
        assert stats['total_dedup_ids_tracked'] == 2
    
    def test_clear_state(self, ingestion, sample_event):
        """Test clearing ingestion state"""
        run = ingestion.ingest(sample_event)
        assert ingestion.processed_count == 1
        assert len(ingestion.runs_queue) == 1
        
        ingestion.clear()
        
        assert ingestion.processed_count == 0
        assert ingestion.deduplicated_count == 0
        assert len(ingestion.runs_queue) == 0
        assert len(ingestion.processed_webhook_ids) == 0
    
    def test_webhook_hash_stability(self, ingestion):
        """Test that webhook hashing is stable"""
        hash1 = ingestion._compute_webhook_hash("webhook-123", "event-hash-456")
        hash2 = ingestion._compute_webhook_hash("webhook-123", "event-hash-456")
        
        assert hash1 == hash2
    
    def test_event_hash_differs_on_different_inputs(self, ingestion):
        """Test that event hashes differ for different events"""
        hash1 = ingestion._compute_event_hash("repo1", "commit1", "build1")
        hash2 = ingestion._compute_event_hash("repo2", "commit1", "build1")
        hash3 = ingestion._compute_event_hash("repo1", "commit2", "build1")
        hash4 = ingestion._compute_event_hash("repo1", "commit1", "build2")
        
        assert hash1 != hash2
        assert hash1 != hash3
        assert hash1 != hash4
        assert hash2 != hash3
        assert hash2 != hash4
        assert hash3 != hash4
    
    def test_queue_preserves_run_data(self, ingestion, sample_event):
        """Test that queue operations preserve Run data"""
        run = ingestion.ingest(sample_event)
        original_id = run.id
        
        queued = ingestion.get_pending_runs()[0]
        assert queued.id == original_id
        assert queued.repo == run.repo
        assert queued.failing_commit == run.failing_commit
        assert queued.failure_logs == run.failure_logs
    
    def test_multiple_dedup_ids_tracked(self, ingestion):
        """Test that multiple webhook IDs are tracked"""
        for i in range(5):
            event = WebhookEvent(
                source="github",
                repo=f"repo-{i}",
                commit=f"commit-{i}",
                branch="main",
                build_id=f"build-{i}",
                failure_logs="Error",
                diff="",
                commit_message="Commit",
                webhook_id=f"webhook-{i}",
                timestamp=datetime.utcnow(),
                metadata={}
            )
            ingestion.ingest(event)
        
        stats = ingestion.get_stats()
        assert stats['total_dedup_ids_tracked'] == 5
        assert stats['processed_count'] == 5
