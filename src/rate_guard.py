"""Rate/Anomaly Guard: Throttles abnormal bursts of failures"""

from typing import Dict, Tuple, Any
from datetime import datetime, timedelta
from collections import deque


class RateAnomalyGuard:
    """
    Tracks event frequency per actor/repo and throttles abnormal bursts.
    
    Protects the system from being flooded or having its budget attacked
    at the volume level (which per-Run budget checks alone can't catch).
    
    Uses sliding-window rate limiting:
    - Track events per time window
    - Flag when frequency exceeds threshold
    - Throttle or escalate abnormal patterns
    """
    
    def __init__(self, window_seconds: int = 60, threshold_per_window: int = 10):
        """
        Initialize rate/anomaly guard.
        
        Args:
            window_seconds: Time window for rate calculations
            threshold_per_window: Max events allowed per window
        """
        self.window_seconds = window_seconds
        self.threshold_per_window = threshold_per_window
        
        # Track events per (repo, actor) tuple
        self.events: Dict[str, deque] = {}  # Key: "repo:actor", Value: deque of timestamps
        self.flagged_actors: Dict[str, str] = {}  # Key: "repo:actor", Value: reason
    
    def _get_key(self, repo: str, actor: str) -> str:
        """Get tracking key for repo/actor"""
        return f"{repo}:{actor}"
    
    def record_event(self, repo: str, actor: str) -> Tuple[bool, str]:
        """
        Record an event from an actor.
        
        Args:
            repo: Repository name
            actor: Actor identifier (e.g., commit author, webhook source)
            
        Returns:
            Tuple of (event_allowed, reason)
        """
        key = self._get_key(repo, actor)
        now = datetime.utcnow()
        
        # Check if actor is already flagged
        if key in self.flagged_actors:
            return False, f"Actor flagged: {self.flagged_actors[key]}"
        
        # Initialize queue if needed
        if key not in self.events:
            self.events[key] = deque()
        
        # Remove old events outside the window
        window_start = now - timedelta(seconds=self.window_seconds)
        while self.events[key] and self.events[key][0] < window_start:
            self.events[key].popleft()
        
        # Check if we're at the threshold BEFORE recording
        if len(self.events[key]) >= self.threshold_per_window:
            reason = f"Rate limit exceeded: {len(self.events[key])} events in {self.window_seconds}s window"
            self.flagged_actors[key] = reason
            return False, reason
        
        # Record the event (only if under threshold)
        self.events[key].append(now)
        
        return True, f"Event recorded ({len(self.events[key])}/{self.threshold_per_window})"
    
    def get_event_rate(self, repo: str, actor: str) -> Dict[str, Any]:
        """
        Get current event rate for a repo/actor.
        
        Args:
            repo: Repository name
            actor: Actor identifier
            
        Returns:
            Dictionary with rate information
        """
        key = self._get_key(repo, actor)
        now = datetime.utcnow()
        
        if key not in self.events:
            return {
                'events_in_window': 0,
                'threshold': self.threshold_per_window,
                'flagged': False,
            }
        
        # Clean up old events
        window_start = now - timedelta(seconds=self.window_seconds)
        while self.events[key] and self.events[key][0] < window_start:
            self.events[key].popleft()
        
        count = len(self.events[key])
        
        return {
            'events_in_window': count,
            'threshold': self.threshold_per_window,
            'percent_of_threshold': (count / self.threshold_per_window * 100),
            'flagged': key in self.flagged_actors,
            'flag_reason': self.flagged_actors.get(key),
        }
    
    def is_actor_flagged(self, repo: str, actor: str) -> bool:
        """
        Check if an actor is currently flagged.
        
        Args:
            repo: Repository name
            actor: Actor identifier
            
        Returns:
            Whether actor is flagged
        """
        key = self._get_key(repo, actor)
        return key in self.flagged_actors
    
    def unflag_actor(self, repo: str, actor: str) -> Tuple[bool, str]:
        """
        Manually unflag an actor (manual override).
        
        Args:
            repo: Repository name
            actor: Actor identifier
            
        Returns:
            Tuple of (success, reason)
        """
        key = self._get_key(repo, actor)
        
        if key not in self.flagged_actors:
            return False, "Actor not flagged"
        
        reason = self.flagged_actors[key]
        del self.flagged_actors[key]
        
        # Clear event history for fresh start
        if key in self.events:
            self.events[key].clear()
        
        return True, f"Unflagged. Previous reason: {reason}"
    
    def detect_anomalies(self, repo: str) -> Dict[str, Any]:
        """
        Detect anomalous patterns for a repo.
        
        Args:
            repo: Repository name
            
        Returns:
            Dictionary with anomaly information
        """
        # Find actors and their rates
        actor_rates = {}
        for key in self.events:
            if key.startswith(repo + ":"):
                actor = key.split(":", 1)[1]
                actor_rates[actor] = self.get_event_rate(repo, actor)
        
        # Detect anomalies
        anomalies = {
            'repo': repo,
            'high_volume_actors': [],
            'flagged_actors': [],
        }
        
        for actor, rate in actor_rates.items():
            if rate['percent_of_threshold'] > 80:
                anomalies['high_volume_actors'].append({
                    'actor': actor,
                    'rate': rate,
                })
            
            if rate['flagged']:
                anomalies['flagged_actors'].append({
                    'actor': actor,
                    'reason': rate['flag_reason'],
                })
        
        return anomalies
    
    def clear_tracking(self, repo: str = None, actor: str = None) -> None:
        """
        Clear tracking data (for testing).
        
        Args:
            repo: Optional repo to clear
            actor: Optional actor to clear
        """
        if repo and actor:
            key = self._get_key(repo, actor)
            if key in self.events:
                del self.events[key]
            if key in self.flagged_actors:
                del self.flagged_actors[key]
        elif repo:
            # Clear all actors for this repo
            keys_to_delete = [k for k in self.events if k.startswith(repo + ":")]
            for key in keys_to_delete:
                if key in self.events:
                    del self.events[key]
                if key in self.flagged_actors:
                    del self.flagged_actors[key]
        else:
            # Clear everything
            self.events.clear()
            self.flagged_actors.clear()
