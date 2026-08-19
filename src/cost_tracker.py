"""Cost tracking for model API calls"""

from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
from src.model_provider import ModelResponse


@dataclass
class CostEntry:
    """Single cost entry for a model call"""
    timestamp: datetime
    provider_name: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    input_cost: float
    output_cost: float
    total_cost: float
    task_type: str  # "semantic", "structural", "attribution", etc.
    success: bool
    retry_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp.isoformat(),
            'provider': self.provider_name,
            'model': self.model_name,
            'tokens': {
                'input': self.prompt_tokens,
                'output': self.completion_tokens,
                'total': self.prompt_tokens + self.completion_tokens,
            },
            'cost': {
                'input': f"${self.input_cost:.6f}",
                'output': f"${self.output_cost:.6f}",
                'total': f"${self.total_cost:.6f}",
            },
            'task': self.task_type,
            'success': self.success,
            'retries': self.retry_count,
        }


@dataclass
class CostStats:
    """Aggregate cost statistics"""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    total_tokens_used: int = 0
    total_cost: float = 0.0
    by_provider: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_task_type: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'summary': {
                'total_calls': self.total_calls,
                'successful': self.successful_calls,
                'failed': self.failed_calls,
                'success_rate': f"{(self.successful_calls / max(self.total_calls, 1)) * 100:.1f}%",
                'total_tokens': self.total_tokens_used,
                'total_cost': f"${self.total_cost:.6f}",
            },
            'by_provider': self.by_provider,
            'by_task': self.by_task_type,
        }


class CostTracker:
    """Track costs for all model API calls"""
    
    def __init__(self):
        """Initialize cost tracker"""
        self.entries: list[CostEntry] = []
        self.stats = CostStats()
    
    def log_call(
        self,
        provider_name: str,
        model_name: str,
        response: ModelResponse,
        task_type: str,
        success: bool,
    ) -> None:
        """
        Log a model API call.
        
        Args:
            provider_name: Name of the provider (e.g., "GroqProvider")
            model_name: Model name used (e.g., "mixtral-8x7b-32768")
            response: ModelResponse with token counts and costs
            task_type: Type of task ("semantic", "structural", etc.)
            success: Whether the call succeeded
        """
        entry = CostEntry(
            timestamp=datetime.utcnow(),
            provider_name=provider_name,
            model_name=model_name,
            prompt_tokens=response.input_tokens,
            completion_tokens=response.output_tokens,
            input_cost=response.estimated_input_cost,
            output_cost=response.estimated_output_cost,
            total_cost=response.total_cost,
            task_type=task_type,
            success=success,
            retry_count=response.retry_count,
        )
        
        self.entries.append(entry)
        self._update_stats(entry)
    
    def _update_stats(self, entry: CostEntry) -> None:
        """Update aggregate statistics"""
        self.stats.total_calls += 1
        if entry.success:
            self.stats.successful_calls += 1
        else:
            self.stats.failed_calls += 1
        
        self.stats.total_tokens_used += entry.prompt_tokens + entry.completion_tokens
        self.stats.total_cost += entry.total_cost
        
        # Track by provider
        if entry.provider_name not in self.stats.by_provider:
            self.stats.by_provider[entry.provider_name] = {
                'calls': 0,
                'tokens': 0,
                'cost': 0.0,
            }
        
        self.stats.by_provider[entry.provider_name]['calls'] += 1
        self.stats.by_provider[entry.provider_name]['tokens'] += entry.prompt_tokens + entry.completion_tokens
        self.stats.by_provider[entry.provider_name]['cost'] += entry.total_cost
        
        # Track by task type
        if entry.task_type not in self.stats.by_task_type:
            self.stats.by_task_type[entry.task_type] = {
                'calls': 0,
                'tokens': 0,
                'cost': 0.0,
                'success_rate': 0.0,
            }
        
        task_stats = self.stats.by_task_type[entry.task_type]
        task_stats['calls'] += 1
        task_stats['tokens'] += entry.prompt_tokens + entry.completion_tokens
        task_stats['cost'] += entry.total_cost
        if task_stats['calls'] > 0:
            successful = sum(1 for e in self.entries if e.task_type == entry.task_type and e.success)
            task_stats['success_rate'] = (successful / task_stats['calls']) * 100
    
    def get_stats(self) -> CostStats:
        """Get current cost statistics"""
        return self.stats
    
    def get_entries(self) -> list[CostEntry]:
        """Get all logged entries"""
        return self.entries.copy()
    
    def get_entries_for_provider(self, provider_name: str) -> list[CostEntry]:
        """Get entries for a specific provider"""
        return [e for e in self.entries if e.provider_name == provider_name]
    
    def get_entries_for_task(self, task_type: str) -> list[CostEntry]:
        """Get entries for a specific task type"""
        return [e for e in self.entries if e.task_type == task_type]
    
    def print_summary(self) -> None:
        """Print human-readable summary"""
        stats = self.get_stats()
        print("\n" + "="*60)
        print("MODEL API COST SUMMARY")
        print("="*60)
        print(f"Total Calls: {stats.total_calls}")
        print(f"Successful: {stats.successful_calls} | Failed: {stats.failed_calls}")
        print(f"Success Rate: {(stats.successful_calls / max(stats.total_calls, 1)) * 100:.1f}%")
        print(f"Total Tokens: {stats.total_tokens_used:,}")
        print(f"Total Cost: ${stats.total_cost:.6f}")
        
        if stats.by_provider:
            print("\nBY PROVIDER:")
            for provider, data in stats.by_provider.items():
                print(f"  {provider}:")
                print(f"    Calls: {data['calls']}")
                print(f"    Tokens: {data['tokens']:,}")
                print(f"    Cost: ${data['cost']:.6f}")
        
        if stats.by_task_type:
            print("\nBY TASK TYPE:")
            for task, data in stats.by_task_type.items():
                print(f"  {task}:")
                print(f"    Calls: {data['calls']}")
                print(f"    Tokens: {data['tokens']:,}")
                print(f"    Cost: ${data['cost']:.6f}")
                print(f"    Success Rate: {data['success_rate']:.1f}%")
        
        print("="*60 + "\n")
    
    def clear(self) -> None:
        """Clear all entries (for testing)"""
        self.entries.clear()
        self.stats = CostStats()
