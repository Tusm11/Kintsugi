"""Budget Guard: Tracks and enforces token/retry/time budgets across runs"""

import os
import time
from typing import Dict, Any, Tuple
from src.models import Run, Cost


class BudgetGuard:
    """
    Centralized budget tracking to prevent budget-leak bugs.
    
    Prevents race conditions that would occur if each handler tracked spend
    independently. Single source of truth for:
    - Tokens consumed (for LLM costs)
    - Retries used (for attempt limits)
    - Wall-clock time spent (for timeout)
    
    All budget operations are atomic.
    """
    
    def __init__(self):
        """Initialize budget guard"""
        self.run_budgets: Dict[str, Dict[str, Any]] = {}
    
    def initialize_budget(self, run: Run) -> None:
        """
        Initialize budget tracking for a run.
        
        Args:
            run: The Run to track budget for
        """
        # Read per-layer retry limits from environment with safe defaults
        max_retries_mechanical = int(os.getenv("MAX_RETRIES_MECHANICAL", str(run.budget.max_retries_mechanical)))
        max_retries_structural = int(os.getenv("MAX_RETRIES_STRUCTURAL", str(run.budget.max_retries_structural)))
        max_retries_semantic = int(os.getenv("MAX_RETRIES_SEMANTIC", str(run.budget.max_retries_semantic)))
        max_retries_total = int(os.getenv("MAX_RETRIES_TOTAL", str(run.budget.max_retries_total)))

        self.run_budgets[run.id] = {
            'max_tokens': run.budget.max_tokens,
            'max_retries_mechanical': max_retries_mechanical,
            'max_retries_structural': max_retries_structural,
            'max_retries_semantic': max_retries_semantic,
            'max_retries_total': max_retries_total,
            'max_wall_clock_ms': run.budget.max_wall_clock_ms,
            'tokens_spent': 0,
            'retries_used_mechanical': 0,
            'retries_used_structural': 0,
            'retries_used_semantic': 0,
            'wall_clock_spent_ms': 0,
            'operations': [],  # Audit trail of budget operations
        }
    
    def charge_tokens(self, run_id: str, tokens: int) -> Tuple[bool, str]:
        """
        Atomically charge tokens to a run's budget.
        
        Args:
            run_id: The Run ID
            tokens: Number of tokens to charge
            
        Returns:
            Tuple of (success, reason)
        """
        if run_id not in self.run_budgets:
            return False, "Run not initialized in budget tracker"
        
        budget = self.run_budgets[run_id]
        new_total = budget['tokens_spent'] + tokens
        
        if new_total > budget['max_tokens']:
            return False, f"Token budget exceeded: {new_total} > {budget['max_tokens']}"
        
        budget['tokens_spent'] = new_total
        budget['operations'].append({
            'type': 'charge_tokens',
            'amount': tokens,
            'new_total': new_total,
        })
        
        return True, f"Charged {tokens} tokens"
    
    def charge_time(self, run_id: str, wall_clock_ms: int) -> Tuple[bool, str]:
        """
        Atomically charge wall-clock time to a run's budget.
        
        Args:
            run_id: The Run ID
            wall_clock_ms: Wall-clock time in milliseconds
            
        Returns:
            Tuple of (success, reason)
        """
        if run_id not in self.run_budgets:
            return False, "Run not initialized in budget tracker"
        
        budget = self.run_budgets[run_id]
        new_total = budget['wall_clock_spent_ms'] + wall_clock_ms
        
        if new_total > budget['max_wall_clock_ms']:
            return False, f"Time budget exceeded: {new_total}ms > {budget['max_wall_clock_ms']}ms"
        
        budget['wall_clock_spent_ms'] = new_total
        budget['operations'].append({
            'type': 'charge_time',
            'amount_ms': wall_clock_ms,
            'new_total_ms': new_total,
        })
        
        return True, f"Charged {wall_clock_ms}ms"
    
    def record_retry(self, run_id: str, layer: str) -> Tuple[bool, str]:
        """
        Atomically record a retry attempt for the specified layer.
        
        Args:
            run_id: The Run ID
            layer: Layer making the retry ('mechanical', 'structural', 'semantic')
            
        Returns:
            Tuple of (success, reason)
        """
        if run_id not in self.run_budgets:
            return False, "Run not initialized in budget tracker"
        
        budget = self.run_budgets[run_id]
        
        # Validate layer
        valid_layers = ['mechanical', 'structural', 'semantic']
        if layer not in valid_layers:
            return False, f"Invalid layer '{layer}'. Must be one of: {valid_layers}"
        
        # Check layer-specific retry budget
        retry_field = f'retries_used_{layer}'
        max_field = f'max_retries_{layer}'
        
        new_retries = budget[retry_field] + 1
        max_retries = budget[max_field]
        
        # Check per-layer limit
        if new_retries > max_retries:
            return False, f"{layer.title()} retry budget exceeded: {new_retries} > {max_retries}"
        
        # Check overall total ceiling (secondary safety net)
        total_retries = budget['retries_used_mechanical'] + budget['retries_used_structural'] + budget['retries_used_semantic'] + 1
        if total_retries > budget['max_retries_total']:
            return False, f"Overall retry budget exceeded: {total_retries} total retries > {budget['max_retries_total']} max allowed"
        
        budget[retry_field] = new_retries
        budget['operations'].append({
            'type': 'retry',
            'layer': layer,
            'count': new_retries,
            'total_retries': total_retries,
        })
        
        return True, f"{layer.title()} retry recorded: {new_retries}/{max_retries} (total: {total_retries}/{budget['max_retries_total']})"
    
    def is_budget_exhausted(self, run_id: str) -> Tuple[bool, str]:
        """
        Check if a run's budget is exhausted.
        
        Args:
            run_id: The Run ID
            
        Returns:
            Tuple of (is_exhausted, reason)
        """
        if run_id not in self.run_budgets:
            return False, "Run not initialized"
        
        budget = self.run_budgets[run_id]
        
        # Check each dimension
        if budget['tokens_spent'] >= budget['max_tokens']:
            return True, f"Token budget exhausted: {budget['tokens_spent']}/{budget['max_tokens']}"
        
        # Check overall total retry ceiling first (primary outer safety net)
        total_retries = budget['retries_used_mechanical'] + budget['retries_used_structural'] + budget['retries_used_semantic']
        if total_retries >= budget['max_retries_total']:
            return True, f"Overall retry budget exhausted: {total_retries}/{budget['max_retries_total']} total retries"
        
        # Check per-layer retry budgets (secondary, layer-specific enforcement)
        for layer in ['mechanical', 'structural', 'semantic']:
            retry_field = f'retries_used_{layer}'
            max_field = f'max_retries_{layer}'
            if budget[retry_field] >= budget[max_field]:
                return True, f"{layer.title()} retry budget exhausted: {budget[retry_field]}/{budget[max_field]}"
        
        if budget['wall_clock_spent_ms'] >= budget['max_wall_clock_ms']:
            return True, f"Time budget exhausted: {budget['wall_clock_spent_ms']}ms/{budget['max_wall_clock_ms']}ms"
        
        return False, "Budget still available"
    
    def get_budget_status(self, run_id: str) -> Dict[str, Any]:
        """
        Get current budget status for a run.
        
        Args:
            run_id: The Run ID
            
        Returns:
            Dictionary with budget status
        """
        if run_id not in self.run_budgets:
            return {'error': 'Run not initialized'}
        
        budget = self.run_budgets[run_id]
        
        return {
            'tokens': {
                'spent': budget['tokens_spent'],
                'max': budget['max_tokens'],
                'remaining': budget['max_tokens'] - budget['tokens_spent'],
                'percent_used': (budget['tokens_spent'] / budget['max_tokens'] * 100) if budget['max_tokens'] > 0 else 0,
            },
            'retries_mechanical': {
                'used': budget['retries_used_mechanical'],
                'max': budget['max_retries_mechanical'],
                'remaining': budget['max_retries_mechanical'] - budget['retries_used_mechanical'],
            },
            'retries_structural': {
                'used': budget['retries_used_structural'],
                'max': budget['max_retries_structural'],
                'remaining': budget['max_retries_structural'] - budget['retries_used_structural'],
            },
            'retries_semantic': {
                'used': budget['retries_used_semantic'],
                'max': budget['max_retries_semantic'],
                'remaining': budget['max_retries_semantic'] - budget['retries_used_semantic'],
            },
            'retries_total': {
                'used': budget['retries_used_mechanical'] + budget['retries_used_structural'] + budget['retries_used_semantic'],
                'max': budget['max_retries_total'],
                'remaining': budget['max_retries_total'] - (budget['retries_used_mechanical'] + budget['retries_used_structural'] + budget['retries_used_semantic']),
            },
            'time_ms': {
                'spent': budget['wall_clock_spent_ms'],
                'max': budget['max_wall_clock_ms'],
                'remaining': budget['max_wall_clock_ms'] - budget['wall_clock_spent_ms'],
                'percent_used': (budget['wall_clock_spent_ms'] / budget['max_wall_clock_ms'] * 100) if budget['max_wall_clock_ms'] > 0 else 0,
            }
        }
    
    def sync_run_budget(self, run: Run) -> None:
        """
        Synchronize run's spent budget from tracker.
        
        Args:
            run: The Run to sync
        """
        if run.id not in self.run_budgets:
            return
        
        budget = self.run_budgets[run.id]
        run.spent.tokens_used = budget['tokens_spent']
        run.spent.wall_clock_ms = budget['wall_clock_spent_ms']
        run.spent.retries_used_mechanical = budget['retries_used_mechanical']
        run.spent.retries_used_structural = budget['retries_used_structural']  
        run.spent.retries_used_semantic = budget['retries_used_semantic']
    
    def clear_budget(self, run_id: str) -> None:
        """
        Clear budget tracking for a run (cleanup).
        
        Args:
            run_id: The Run ID
        """
        if run_id in self.run_budgets:
            del self.run_budgets[run_id]
