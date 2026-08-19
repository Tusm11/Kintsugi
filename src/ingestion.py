"""Ingestion Layer: Receives CI failure events and creates Runs"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from src.models import Run, Step, StepType, StepLayer, StepStatus


@dataclass
class WebhookEvent:
    """Represents a CI failure webhook event"""
    source: str  # "github", "gitlab", "circleci", etc.
    repo: str
    commit: str
    branch: str
    build_id: str
    failure_logs: str
    diff: str
    commit_message: str
    webhook_id: str  # For deduplication
    timestamp: datetime
    metadata: Dict[str, Any]


class IngestionLayer:
    """
    Handles CI failure event ingestion, deduplication, and Run creation.
    
    Responsibilities:
    - Receive webhook events from various CI systems
    - Deduplicate retried deliveries using webhook_id
    - Create Run objects from events
    - Place runs in a queue for downstream processing
    """
    
    def __init__(self):
        """Initialize the ingestion layer"""
        self.processed_webhook_ids: set = set()  # Track webhook IDs for retry dedup
        self.processed_event_hashes: set = set()  # Track event hashes for build dedup
        self.runs_queue: List[Run] = []  # Simple queue (would use Redis Streams in prod)
        self.processed_count = 0
        self.deduplicated_count = 0
    
    def _compute_webhook_hash(self, webhook_id: str, event_hash: str) -> str:
        """
        Compute a stable hash for webhook deduplication.
        
        Combines webhook delivery ID with event content hash to handle
        both retried deliveries and duplicate events.
        """
        combined = f"{webhook_id}:{event_hash}"
        return hashlib.sha256(combined.encode()).hexdigest()
    
    def _compute_event_hash(self, repo: str, commit: str, build_id: str) -> str:
        """
        Compute hash of event content to detect duplicates beyond retries.
        
        Uses repo, commit, and build_id as the stable identifier for the event.
        """
        event_key = f"{repo}:{commit}:{build_id}"
        return hashlib.sha256(event_key.encode()).hexdigest()
    
    def is_duplicate(self, webhook_id: str, repo: str, commit: str, build_id: str) -> bool:
        """
        Check if this event has already been processed.
        
        Uses two-layer dedup:
        1. webhook_id - detects same webhook retry delivery
        2. event_hash (repo+commit+build_id) - detects same build even with different webhook_id
        
        Returns True if either dedup condition matches, False if it's a new event.
        """
        # Check if we've seen this exact webhook delivery before (retry detection)
        if webhook_id in self.processed_webhook_ids:
            return True
        
        # Check if we've seen this build before (event_hash fallback)
        event_hash = self._compute_event_hash(repo, commit, build_id)
        if event_hash in self.processed_event_hashes:
            return True
        
        return False
    
    def ingest(self, event: WebhookEvent) -> Optional[Run]:
        """
        Process a webhook event and create a Run if it's not a duplicate.
        
        Args:
            event: WebhookEvent containing CI failure information
            
        Returns:
            Run object if event is new and processed successfully, None if duplicate
        """
        # Check for duplicates
        if self.is_duplicate(event.webhook_id, event.repo, event.commit, event.build_id):
            self.deduplicated_count += 1
            return None
        
        # Record that we've processed this webhook ID (retry dedup)
        self.processed_webhook_ids.add(event.webhook_id)
        
        # Record that we've processed this event (build dedup with fallback)
        event_hash = self._compute_event_hash(event.repo, event.commit, event.build_id)
        self.processed_event_hashes.add(event_hash)
        
        # Create a new Run
        run = self._create_run_from_event(event)
        
        # Add to queue
        self.runs_queue.append(run)
        self.processed_count += 1
        
        return run
    
    def _create_run_from_event(self, event: WebhookEvent) -> Run:
        """Convert a webhook event into a Run object"""
        run = Run(
            repo=event.repo,
            failing_commit=event.commit,
            failure_logs=event.failure_logs,
            diff=event.diff,
            commit_message=event.commit_message,
            metadata={
                'source': event.source,
                'branch': event.branch,
                'build_id': event.build_id,
                'webhook_id': event.webhook_id,
                'event_timestamp': event.timestamp.isoformat(),
                **event.metadata
            }
        )
        
        # Add an initial ingestion step for audit trail
        ingestion_step = Step(
            type=StepType.VERIFICATION,  # Mark as ingestion step
            layer=StepLayer.MECHANICAL,
            status=StepStatus.SUCCESS,
            input={
                'source': event.source,
                'repo': event.repo,
                'commit': event.commit,
                'build_id': event.build_id,
            },
            output={
                'run_id': run.id,
                'ingested_at': datetime.utcnow().isoformat(),
            }
        )
        run.add_step(ingestion_step)
        
        return run
    
    def get_pending_runs(self) -> List[Run]:
        """
        Get all pending runs from the queue.
        
        Returns:
            List of Run objects waiting for processing
        """
        return self.runs_queue.copy()
    
    def dequeue_run(self) -> Optional[Run]:
        """
        Remove and return the first run from the queue.
        
        Returns:
            Run object if queue is not empty, None otherwise
        """
        if not self.runs_queue:
            return None
        return self.runs_queue.pop(0)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get ingestion statistics.
        
        Returns:
            Dictionary with processing stats
        """
        return {
            'processed_count': self.processed_count,
            'deduplicated_count': self.deduplicated_count,
            'pending_runs': len(self.runs_queue),
            'webhook_ids_tracked': len(self.processed_webhook_ids),
            'event_hashes_tracked': len(self.processed_event_hashes),
        }
    
    def clear(self) -> None:
        """
        Clear all state. Useful for testing.
        
        WARNING: This clears deduplication tracking, so events could be reprocessed.
        """
        self.processed_webhook_ids.clear()
        self.processed_event_hashes.clear()
        self.runs_queue.clear()
        self.processed_count = 0
        self.deduplicated_count = 0
