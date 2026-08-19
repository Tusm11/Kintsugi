"""Core data model for Kintsugi: Run and Step classes"""

from enum import Enum
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass, field, asdict
import uuid


class StepType(str, Enum):
    """Types of steps in the pipeline"""
    ATTRIBUTION = "attribution"
    REPAIR = "repair"
    VERIFICATION = "verification"


class StepLayer(str, Enum):
    """Failure layers"""
    MECHANICAL = "mechanical"
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"


class StepStatus(str, Enum):
    """Status of a step"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ESCALATED = "escalated"


class RunStatus(str, Enum):
    """Status of a run"""
    IN_PROGRESS = "in_progress"
    HEALED = "healed"
    ESCALATED = "escalated"
    FAILED = "failed"


@dataclass
class Cost:
    """Cost metrics for a step or run - per-layer retry tracking"""
    tokens_used: int = 0
    wall_clock_ms: int = 0
    retries_used_mechanical: int = 0
    retries_used_structural: int = 0
    retries_used_semantic: int = 0
    
    def to_dict(self) -> Dict[str, int]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, int]) -> "Cost":
        return cls(**data)


@dataclass
class Budget:
    """Budget constraints for a run - per-layer retry limits with overall ceiling"""
    max_tokens: int = 10000
    max_retries_mechanical: int = 5    # Mechanical retries are cheap (no LLM calls)
    max_retries_structural: int = 3    # Structural retries use SLM, moderate cost  
    max_retries_semantic: int = 2      # Semantic retries use LLM, expensive
    max_retries_total: int = 7         # Overall ceiling: tighter than sum (5+3+2=10) to force escalation if Run churns across multiple layers
    max_wall_clock_ms: int = 300000    # 5 minutes
    
    def to_dict(self) -> Dict[str, int]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, int]) -> "Budget":
        return cls(**data)


@dataclass
class Attribution:
    """Structured attribution for semantic failures with source tracking"""
    claimed_cause: str
    evidence_for: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    alternatives_considered: List[Dict[str, str]] = field(default_factory=list)
    counterfactual_result: Optional[str] = None  # "pass", "fail", or "inconclusive"
    attribution_source: str = "model"  # "model" = real LLM reasoning, "fallback_heuristic" = pattern matching (not auto-apply eligible)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Attribution":
        # Handle legacy field: evidence_strength_source (removed, no longer needed)
        # Backward compatibility: silently drop it if present in serialized data
        if 'evidence_strength_source' in data:
            data = {k: v for k, v in data.items() if k != 'evidence_strength_source'}
        return cls(**data)


@dataclass
class Step:
    """A single step in the repair pipeline"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: StepType = StepType.ATTRIBUTION
    layer: StepLayer = StepLayer.MECHANICAL
    input: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    cost: Cost = field(default_factory=Cost)
    attribution: Optional[Attribution] = None
    error_message: Optional[str] = None
    timestamp_created: datetime = field(default_factory=datetime.utcnow)
    timestamp_started: Optional[datetime] = None
    timestamp_completed: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['type'] = self.type.value
        data['layer'] = self.layer.value
        data['status'] = self.status.value
        data['cost'] = self.cost.to_dict()
        if self.attribution:
            data['attribution'] = self.attribution.to_dict()
        data['timestamp_created'] = self.timestamp_created.isoformat()
        data['timestamp_started'] = self.timestamp_started.isoformat() if self.timestamp_started else None
        data['timestamp_completed'] = self.timestamp_completed.isoformat() if self.timestamp_completed else None
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Step":
        data_copy = data.copy()
        data_copy['type'] = StepType(data_copy['type'])
        data_copy['layer'] = StepLayer(data_copy['layer'])
        data_copy['status'] = StepStatus(data_copy['status'])
        data_copy['cost'] = Cost.from_dict(data_copy['cost'])
        if data_copy.get('attribution'):
            data_copy['attribution'] = Attribution.from_dict(data_copy['attribution'])
        data_copy['timestamp_created'] = datetime.fromisoformat(data_copy['timestamp_created'])
        if data_copy.get('timestamp_started'):
            data_copy['timestamp_started'] = datetime.fromisoformat(data_copy['timestamp_started'])
        if data_copy.get('timestamp_completed'):
            data_copy['timestamp_completed'] = datetime.fromisoformat(data_copy['timestamp_completed'])
        return cls(**data_copy)


@dataclass
class ScopeConfig:
    """Configuration for Scope Guard limits per repo/org"""
    max_files_touched: int = 5          # Safe default: 5 files
    max_lines_changed: int = 100        # Safe default: 100 lines
    allow_protected_paths_override: bool = False  # Must be explicit
    protected_paths_override_reason: str = ""  # Audit trail for why override was set
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScopeConfig":
        return cls(**data)
    
    @classmethod
    def safe_defaults(cls) -> "ScopeConfig":
        """Return safe default configuration"""
        return cls(
            max_files_touched=5,
            max_lines_changed=100,
            allow_protected_paths_override=False,
            protected_paths_override_reason=""
        )


@dataclass
class Run:
    """A CI failure repair run"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    repo: str = ""
    failing_commit: str = ""
    steps: List[Step] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    spent: Cost = field(default_factory=Cost)
    final_status: RunStatus = RunStatus.IN_PROGRESS
    failure_logs: str = ""
    diff: str = ""
    commit_message: str = ""
    timestamp_created: datetime = field(default_factory=datetime.utcnow)
    timestamp_completed: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['steps'] = [step.to_dict() if isinstance(step, Step) else step for step in self.steps]
        data['budget'] = self.budget.to_dict()
        data['spent'] = self.spent.to_dict()
        data['final_status'] = self.final_status.value
        data['timestamp_created'] = self.timestamp_created.isoformat()
        data['timestamp_completed'] = self.timestamp_completed.isoformat() if self.timestamp_completed else None
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Run":
        data_copy = data.copy()
        data_copy['steps'] = [Step.from_dict(step) if isinstance(step, dict) else step for step in data_copy.get('steps', [])]
        data_copy['budget'] = Budget.from_dict(data_copy['budget'])
        data_copy['spent'] = Cost.from_dict(data_copy['spent'])
        data_copy['final_status'] = RunStatus(data_copy['final_status'])
        data_copy['timestamp_created'] = datetime.fromisoformat(data_copy['timestamp_created'])
        if data_copy.get('timestamp_completed'):
            data_copy['timestamp_completed'] = datetime.fromisoformat(data_copy['timestamp_completed'])
        return cls(**data_copy)
    
    def add_step(self, step: Step) -> None:
        """Add a step to this run"""
        self.steps.append(step)
    
    def get_last_step(self) -> Optional[Step]:
        """Get the most recent step"""
        return self.steps[-1] if self.steps else None
    
    def is_budget_exceeded(self) -> bool:
        """Check if budget has been exceeded"""
        return (
            self.spent.tokens_used >= self.budget.max_tokens or
            self.spent.wall_clock_ms >= self.budget.max_wall_clock_ms
        )
