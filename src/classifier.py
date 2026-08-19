"""Classifier: Determines failure type (mechanical, structural, semantic)"""

import re
from typing import Dict, Any, List, Tuple
from src.models import Run, Step, StepType, StepLayer, StepStatus


class FailurePattern:
    """
    Represents a pattern to detect a specific type of failure.
    
    Uses regex matching against failure logs to identify failure types.
    """
    
    def __init__(self, name: str, layer: StepLayer, patterns: List[str], exit_codes: List[int] = None):
        """
        Initialize a failure pattern.
        
        Args:
            name: Name/description of this pattern
            layer: The layer this pattern indicates (mechanical, structural, semantic)
            patterns: List of regex patterns to match in logs
            exit_codes: Optional list of exit codes that indicate this pattern
        """
        self.name = name
        self.layer = layer
        self.patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
        self.exit_codes = exit_codes or []
    
    def matches(self, logs: str, exit_code: int = None) -> bool:
        """
        Check if this pattern matches the failure.
        
        Args:
            logs: Failure log text
            exit_code: Exit code from failed process
            
        Returns:
            True if pattern matches, False otherwise
        """
        # Check exit code match if specified
        if self.exit_codes and exit_code is not None:
            if exit_code in self.exit_codes:
                return True
        
        # Check log pattern matches
        for pattern in self.patterns:
            if pattern.search(logs):
                return True
        
        return False


class Classifier:
    """
    Deterministic, rule-based classifier for CI failures.
    
    Assigns each failure to one of three layers:
    - MECHANICAL: Infrastructure failures (timeouts, dead endpoints, rate limits)
    - STRUCTURAL: Malformed output/config that a validator can catch
    - SEMANTIC: Logic failures, tests fail but nothing "looks" wrong
    
    This component uses NO LLM — it's purely rule-based to keep it fast and cost-free.
    """
    
    def __init__(self):
        """Initialize classifier with built-in patterns"""
        self.patterns = self._build_patterns()
    
    def _build_patterns(self) -> List[FailurePattern]:
        """
        Build the standard failure patterns.
        
        Returns:
            List of FailurePattern objects
        """
        patterns = []
        
        # MECHANICAL: Network/Infrastructure failures (highest priority - cheapest to fix)
        patterns.append(FailurePattern(
            name="Connection Timeout",
            layer=StepLayer.MECHANICAL,
            patterns=[
                r"(?:connection|network).*timeout",
                r"timeout.*(?:connection|network)",
                r"504.*gateway",
                r"connection.*refused",
                r"network.*unreachable",
            ],
            exit_codes=[124, 129]  # Common timeout exit codes
        ))
        
        patterns.append(FailurePattern(
            name="Rate Limit",
            layer=StepLayer.MECHANICAL,
            patterns=[
                r"(?:rate|request).*limit",
                r"429.*too.*many",
                r"throttled",
                r"quota.*exceeded",
            ],
            exit_codes=[]
        ))
        
        patterns.append(FailurePattern(
            name="Service Unavailable",
            layer=StepLayer.MECHANICAL,
            patterns=[
                r"503.*service.*unavailable",
                r"service.*down",
                r"temporarily.*unavailable",
                r"(?:api|server).*down",
            ],
            exit_codes=[]
        ))
        
        patterns.append(FailurePattern(
            name="Flaky Network",
            layer=StepLayer.MECHANICAL,
            patterns=[
                r"connection.*reset",
                r"broken.*pipe",
                r"intermittent.*failure",
                r"(?:dns|name).*resolution.*failed",
            ],
            exit_codes=[]
        ))
        
        # SEMANTIC: Logic failures - test failures that aren't infrastructure (check BEFORE structural)
        # These are more specific than structural, so they should be checked first
        patterns.append(FailurePattern(
            name="Test Assertion Failed",
            layer=StepLayer.SEMANTIC,
            patterns=[
                r"(?:assertion|assert).*(?:failed|error)",
                r"expected.*but.*got",
                r"equal.*assertion",
                r"match.*failed",
                r"(?:not|should).*be.*null",
            ],
            exit_codes=[]
        ))
        
        patterns.append(FailurePattern(
            name="Test Timeout",
            layer=StepLayer.SEMANTIC,
            patterns=[
                r"test.*timeout",
                r"jest.*timeout",
                r"(?:mocha|pytest|jest).*exceeded.*timeout",
            ],
            exit_codes=[124]
        ))
        
        patterns.append(FailurePattern(
            name="Exception in Test",
            layer=StepLayer.SEMANTIC,
            patterns=[
                # Specific exception types (not just "error")
                r"(?:nullpointerexception|typeerror|attributeerror|keyerror|indexerror|valueerror)",
                # Real Python traceback format
                r"traceback.*(?:most recent|from line)",
                # Stack trace patterns
                r"(?:at|in)\s+(?:line|function|method)\s+\d+",
                # Exception with message
                r"(?:exception|error):\s+\w+(?:error|exception)",
            ],
            exit_codes=[]
        ))
        
        patterns.append(FailurePattern(
            name="Logic Error",
            layer=StepLayer.SEMANTIC,
            patterns=[
                r"(?:returned|got).*(?:wrong|unexpected)",
                r"(?:incorrect|wrong).*(?:output|result|value)",
                r"(?:logic|business).*(?:error|bug)",
                r"failed.*(?:expectation|condition)",
            ],
            exit_codes=[]
        ))
        
        # STRUCTURAL: Malformed output/config (checked AFTER semantic)
        patterns.append(FailurePattern(
            name="YAML Syntax Error",
            layer=StepLayer.STRUCTURAL,
            patterns=[
                r"yaml.*(?:error|parse|syntax)",
                r"yml.*(?:error|parse|syntax)",
                r"invalid.*yaml",
                r"mapping.*values.*not.*allowed",
                r"did.*not.*find.*expected.*key",
            ],
            exit_codes=[1]
        ))
        
        patterns.append(FailurePattern(
            name="JSON Parse Error",
            layer=StepLayer.STRUCTURAL,
            patterns=[
                r"json.*(?:decode|parse|error|syntax)",
                r"invalid.*json",
                r"unexpected.*(?:end|character|token).*(?:of|in).*json",
                r"json.*parse.*failed",
            ],
            exit_codes=[1]
        ))
        
        patterns.append(FailurePattern(
            name="Lint/Format Violation",
            layer=StepLayer.STRUCTURAL,
            patterns=[
                # More specific: lint tool name + error/violation
                r"(?:eslint|pylint|flake8|rustfmt).*(?:error|warning|violation)",
                r"(?:eslint|pylint|flake8|rustfmt).*(?:failed|check)",
                r"linting.*(?:error|failed)",
                r"code.*style.*(?:error|violation)",
                # Specific code style issues
                r"line.*too.*long",
                r"unused.*(?:import|variable|parameter)",
            ],
            exit_codes=[1]
        ))
        
        patterns.append(FailurePattern(
            name="Compilation Error",
            layer=StepLayer.STRUCTURAL,
            patterns=[
                # Compiler-specific
                r"(?:typescript|java|go|rust).*(?:compile|build).*(?:error|failed)",
                # Type errors (but not generic "TypeError")
                r"type.*mismatch",
                r"cannot.*assign.*to.*(?:variable|field|parameter)",
                r"undefined.*(?:reference|symbol|variable)",
                r"(?:expected|found).*(?:type|class|interface)",
            ],
            exit_codes=[1]
        ))
        
        patterns.append(FailurePattern(
            name="Dependency Resolution Error",
            layer=StepLayer.STRUCTURAL,
            patterns=[
                r"(?:npm|pip|cargo|yarn).*(?:install|resolve).*(?:failed|error)",
                r"(?:npm|pip|cargo).*(?:ERR|error)",
                r"no.*matching.*(?:version|package)",
                r"dependency.*(?:conflict|mismatch)",
                r"requirement.*not.*satisfied",
                r"(?:package|module).*(?:not.*found|not.*installed)",
            ],
            exit_codes=[1]
        ))
        
        return patterns
    
    def classify(self, run: Run) -> Tuple[StepLayer, str]:
        """
        Classify a run's failure into a failure layer.
        
        Args:
            run: The Run object to classify
            
        Returns:
            Tuple of (StepLayer, reason_string)
        """
        logs = run.failure_logs
        exit_code = run.metadata.get('exit_code')
        
        # Extract exit code from logs if not in metadata
        if exit_code is None:
            exit_code = self._extract_exit_code(logs)
        
        # Try to match patterns in order: mechanical, structural, semantic
        # Mechanical patterns have highest priority (cheapest to fix)
        for pattern in self.patterns:
            if pattern.matches(logs, exit_code):
                return pattern.layer, f"Matched pattern: {pattern.name}"
        
        # Default to semantic if no pattern matches
        # (Something is wrong with the test/code logic)
        return StepLayer.SEMANTIC, "No specific pattern matched - treating as semantic failure"
    
    def _extract_exit_code(self, logs: str) -> int:
        """
        Attempt to extract exit code from logs.
        
        Args:
            logs: Log text to search
            
        Returns:
            Exit code if found, otherwise 1 (generic failure)
        """
        # Look for common exit code patterns
        patterns = [
            r"exit.*code.*:?\s*(\d+)",
            r"exited.*with.*(?:code|status).*:?\s*(\d+)",
            r"status.*code.*:?\s*(\d+)",
            r"process.*exit.*(\d+)",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, logs, re.IGNORECASE)
            if match:
                try:
                    return int(match.group(1))
                except (ValueError, IndexError):
                    pass
        
        return 1  # Default exit code for failure
    
    def create_classification_step(self, run: Run, layer: StepLayer, reason: str) -> Step:
        """
        Create a classification step to add to the run's audit trail.
        
        Args:
            run: The Run being classified
            layer: The classified layer
            reason: Reason for classification
            
        Returns:
            Step object representing the classification
        """
        step = Step(
            type=StepType.ATTRIBUTION,
            layer=layer,
            status=StepStatus.SUCCESS,
            input={
                'failure_logs': run.failure_logs[:500],  # First 500 chars
                'commit_message': run.commit_message,
                'metadata': run.metadata,
            },
            output={
                'classified_layer': layer.value,
                'reason': reason,
            }
        )
        return step
    
    def add_classification_to_run(self, run: Run, layer: StepLayer, reason: str) -> None:
        """
        Add a classification step to a run.
        
        Args:
            run: The Run to add classification to
            layer: The classified layer
            reason: Reason for classification
        """
        step = self.create_classification_step(run, layer, reason)
        run.add_step(step)
