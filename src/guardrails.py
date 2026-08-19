"""Guardrails: Input/Output scanning, Confidence Gate, Scope Guard"""

import re
from typing import Dict, Any, List, Tuple, Optional
from src.models import Run, Step, StepType, StepLayer, StepStatus, Attribution, ScopeConfig


class ScopeConfigManager:
    """
    Manages Scope Guard configuration per repo/org.
    
    Enforces separation between:
    - Numeric limits (max_files, max_lines) - configurable per repo
    - Protected paths - hardcoded, only overridable with explicit flag
    
    Never fails open. Missing configs default to safe limits.
    
    **IMPORTANT: Configuration Access Control**
    
    This manager itself does NOT enforce who can call set_config().
    It is the responsibility of the caller to ensure:
    
    - set_config() is only called from OUTSIDE the pipeline (config files, admin endpoints)
    - Runs cannot call set_config() at runtime
    - Skills, handlers, and Action Layer cannot write to repo config
    
    If any code path from inside the pipeline reaches set_config(), the protection
    is weaker than it appears. This manager provides the data structure and
    defaults, not the access control boundary.
    
    Set allow_protected_paths_override only through:
    - Direct config file edits (checked into version control)
    - Admin-only API endpoints (outside the pipeline)
    - Manual approval processes
    
    NOT through:
    - Skill outputs
    - Handler decisions
    - Action Layer decisions
    - Run metadata
    """
    
    def __init__(self):
        """Initialize config manager"""
        self.configs: Dict[str, ScopeConfig] = {}  # Key: "repo" or "org:repo"
    
    def set_config(self, repo_or_org: str, config: ScopeConfig) -> None:
        """
        Set configuration for a repo or org.
        
        Args:
            repo_or_org: Repository identifier (e.g., "user/repo" or "org:")
            config: ScopeConfig to apply
        """
        self.configs[repo_or_org] = config
    
    def get_config(self, repo: str, org: Optional[str] = None) -> ScopeConfig:
        """
        Get configuration for a repo, with fallback to org, then global defaults.
        
        Args:
            repo: Repository identifier
            org: Optional org identifier
            
        Returns:
            ScopeConfig with repo-specific, org-specific, or safe defaults
        """
        # Try repo-specific first
        if repo in self.configs:
            return self.configs[repo]
        
        # Try org-level second
        if org:
            org_key = f"{org}:"
            if org_key in self.configs:
                return self.configs[org_key]
        
        # Fall back to safe defaults (never fail open)
        return ScopeConfig.safe_defaults()
    
    def validate_protected_paths_override(self, config: ScopeConfig) -> Tuple[bool, str]:
        """
        Validate that protected_paths override has proper justification.
        
        **Soft validation only** — checks length (>10 chars) to prevent empty or trivial
        reasons like "asdfasdfasdf", but does NOT validate semantic content.
        A substantive-sounding reason is required by the config model itself
        (enforced by humans setting the config), not by this function.
        
        This is a guardrail against obvious mistakes, not a cryptographic proof.
        
        Args:
            config: ScopeConfig to validate
            
        Returns:
            Tuple of (is_valid, reason)
        """
        if config.allow_protected_paths_override:
            if not config.protected_paths_override_reason:
                return False, "protected_paths_override requires explicit reason"
            
            if len(config.protected_paths_override_reason) < 10:
                return False, "protected_paths_override reason must be substantive (>10 chars)"
        
        return True, "Config valid"
    
    def clear_config(self, repo_or_org: str) -> None:
        """
        Clear configuration for a repo/org (revert to defaults).
        
        Args:
            repo_or_org: Repository or org identifier
        """
        if repo_or_org in self.configs:
            del self.configs[repo_or_org]


class InputGuardrail:
    """
    Scans externally-sourced content before it reaches any LLM.
    
    Protects against prompt injection:
    - Code comments attempting to override instructions
    - Commit messages with malicious content
    - Logs with injected instructions
    
    Uses pattern-based detection for common injection techniques.
    Adopted from open-source scanners like LLM Guard or Llama Guard.
    """
    
    def __init__(self):
        """Initialize input guardrail"""
        # Common injection patterns
        self.injection_patterns = [
            r"ignore.*previous.*instructions",
            r"forget.*everything",
            r"system.*prompt",
            r"instructions.*override",
            r"tell.*me.*secret",
            r"bypass.*security",
            r"execute.*(?:code|shell|command|script)",
            r"run.*(?:code|shell|command|script)",
            r"//.*\{.*\{",  # Template injection
            r"\$\{.*\}",    # Variable injection
        ]
    
    def scan(self, content: str) -> Tuple[bool, str]:
        """
        Scan content for injection attempts.
        
        Args:
            content: Text to scan
            
        Returns:
            Tuple of (is_safe, reason)
        """
        # Check for injection patterns
        for pattern in self.injection_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return False, f"Potential injection detected: {pattern}"
        
        # Check for suspicious encoding or escape sequences
        if "\\x" in content or "\\u00" in content:
            # Could be obfuscated injection
            if re.search(r"\\x[0-9a-fA-F]{2}", content):
                return False, "Suspicious hex encoding detected"
        
        return True, "Content is safe"
    
    def scan_run(self, run: Run) -> Tuple[bool, str]:
        """
        Scan all externally-sourced content in a Run.
        
        Args:
            run: The Run to scan
            
        Returns:
            Tuple of (is_safe, reason)
        """
        # Scan logs
        is_safe, reason = self.scan(run.failure_logs)
        if not is_safe:
            return False, f"Failure logs: {reason}"
        
        # Scan commit message
        is_safe, reason = self.scan(run.commit_message)
        if not is_safe:
            return False, f"Commit message: {reason}"
        
        # Scan diff (for inline comments)
        is_safe, reason = self.scan(run.diff)
        if not is_safe:
            return False, f"Diff: {reason}"
        
        return True, "All content is safe"


class OutputGuardrail:
    """
    Scans generated fixes before they're applied.
    
    Checks for:
    - Leaked secrets (API keys, credentials)
    - Unsafe patterns (code that weakens security)
    - Weakened tests (changes that make tests always pass)
    - Suspicious code patterns
    
    Adopted from open-source secret scanners and linters.
    """
    
    def __init__(self):
        """Initialize output guardrail"""
        # Secret patterns
        self.secret_patterns = [
            r"(?:api[_-]?)?key['\"]?\s*[:=]\s*['\"]?[a-zA-Z0-9\-_]{32,}",
            r"(?:password|passwd|pwd)['\"]?\s*[:=]\s*['\"]?.{4,}",
            r"(?:token|bearer)['\"]?\s*[:=]\s*['\"]?.{20,}",
            r"aws[_-]?secret",
            r"private[_-]?key",
        ]
        
        # Unsafe patterns
        self.unsafe_patterns = [
            r"disable.*ssl.*verify",
            r"insecure.*true",
            r"skip.*verification",
            r"os\.system\(",
            r"eval\(",
            r"exec\(",
        ]
        
        # Test-weakening patterns
        self.test_weakening_patterns = [
            r"pass\s*#.*skip",
            r"return\s+True\s*#.*always",
            r"assert\s+True",  # Unconditional pass
            r"skip\(.*\)",
        ]
    
    def scan(self, content: str) -> Tuple[bool, str]:
        """
        Scan content for secrets and unsafe patterns.
        
        Args:
            content: Code or content to scan
            
        Returns:
            Tuple of (is_safe, reason)
        """
        # Check for secrets
        for pattern in self.secret_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return False, "Possible secret in output"
        
        # Check for unsafe patterns
        for pattern in self.unsafe_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return False, f"Unsafe pattern detected: {pattern}"
        
        # Check for test-weakening patterns
        for pattern in self.test_weakening_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return False, f"Test-weakening pattern detected: {pattern}"
        
        return True, "Content appears safe"
    
    def scan_repair_output(self, fix_description: str, code_changes: str) -> Tuple[bool, str]:
        """
        Scan repair output before application.
        
        Args:
            fix_description: Description of the fix
            code_changes: The actual code changes
            
        Returns:
            Tuple of (is_safe, reason)
        """
        # Scan the code changes
        is_safe, reason = self.scan(code_changes)
        if not is_safe:
            return False, f"Code changes: {reason}"
        
        # Scan the description (could hide intent)
        is_safe, reason = self.scan(fix_description)
        if not is_safe:
            return False, f"Description: {reason}"
        
        return True, "Repair output appears safe"


class ConfidenceGate:
    """
    Decides whether a proposed fix is eligible for automatic application.
    
    NOT a threshold on a float score (which is easy to game and hard to audit).
    Instead, uses STRUCTURED EVIDENCE RULES:
    
    auto_apply_eligible = (
        counterfactual_result == "pass"
        AND evidence_against is empty
        AND all alternatives were explicitly rejected with a stated reason
    )
    
    If evidence is inconclusive, escalates to human review.
    If evidence contradicts the fix, escalates to human review.
    """
    
    def __init__(self):
        """Initialize confidence gate"""
        pass
    
    def is_eligible_for_auto_apply(self, run: Run) -> Tuple[bool, str]:
        """
        Determine if a run is eligible for automatic application.
        
        Args:
            run: The Run to evaluate
            
        Returns:
            Tuple of (is_eligible, reason)
        """
        # Get the attribution step
        attribution_step = self._get_attribution_step(run)
        
        if not attribution_step or not attribution_step.attribution:
            return False, "No attribution evidence available"
        
        attr = attribution_step.attribution
        
        # Check 0: Attribution must be from real model, not fallback heuristics
        if attr.attribution_source == "fallback_heuristic":
            return False, "Attribution based on fallback heuristics, not real model reasoning — ineligible for auto-apply"
        
        # Check 1: Counterfactual result must indicate pass
        if attr.counterfactual_result != "pass":
            return False, f"Counterfactual inconclusive: {attr.counterfactual_result}"
        
        # Check 2: No evidence against the fix
        if attr.evidence_against and len(attr.evidence_against) > 0:
            reasons = "; ".join(attr.evidence_against[:2])  # First 2 reasons
            return False, f"Evidence contradicts fix: {reasons}"
        
        # Check 3: All alternatives must have stated rejection reasons
        for alt in attr.alternatives_considered:
            if not alt.get('why_rejected'):
                return False, f"Alternative '{alt.get('cause')}' was not explicitly rejected"
        
        return True, "All evidence criteria met for auto-apply (model-sourced attribution)"
    
    def _get_attribution_step(self, run: Run) -> Optional[Step]:
        """Get the most recent attribution step"""
        for step in reversed(run.steps):
            if step.type == StepType.ATTRIBUTION and step.attribution:
                return step
        return None
    
    def create_gate_decision_step(self, run: Run, eligible: bool, reason: str) -> Step:
        """
        Create a confidence gate decision step.
        
        Args:
            run: The Run being evaluated
            eligible: Whether eligible for auto-apply
            reason: Reason for decision
            
        Returns:
            Step object representing the decision
        """
        step = Step(
            type=StepType.VERIFICATION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            input={'attribution_present': bool(self._get_attribution_step(run))},
            output={
                'auto_apply_eligible': eligible,
                'reasoning': reason,
            }
        )
        return step


class ScopeGuard:
    """
    Hard, fixed safety rules for automatic patch application.
    
    Separates two concerns:
    1. Numeric limits (max_files, max_lines) - configurable per repo via ScopeConfig
    2. Protected file patterns - hardcoded, only overridable with explicit flag
    
    Rules that CANNOT be overridden by confidence scores:
    1. Patches exceeding configured file/line limits automatically escalate
    2. Patches modifying protected files (CI, tests, security) automatically escalate
       - unless allow_protected_paths_override flag is explicitly set AND justified
    
    Never fails open - missing configs default to safe limits.
    """
    
    # Protected file patterns - HARDCODED, not in config
    # These can only be overridden with explicit allow_protected_paths_override flag
    PROTECTED_FILE_PATTERNS = [
        r"\.github.*workflows",
        r"\.gitlab.*ci",
        r"\.circleci",
        r"jenkinsfile",
        r"buildfile",
        r"docker.*compose",
        r"test.*\.py$",
        r"test.*\.ts$",
        r"test.*\.js$",
        r"__init__\.py",  # Module initialization
        r"setup\.py",
        r"pyproject\.toml",
        r"package\.json",
        r"security",
        r"auth",
        r"crypto",
    ]
    
    def __init__(self, config_manager: Optional[ScopeConfigManager] = None):
        """
        Initialize scope guard.
        
        Args:
            config_manager: Optional ScopeConfigManager for per-repo config
        """
        self.config_manager = config_manager or ScopeConfigManager()
    
    def is_in_scope_for_auto_apply(self, run: Run, repo: str = None, org: str = None) -> Tuple[bool, str]:
        """
        Check if a patch is within safe scope for auto-apply.
        
        Uses per-repo configuration for numeric limits.
        Protected file patterns are always enforced.
        
        Args:
            run: The Run with the proposed patch
            repo: Optional repo identifier (for config lookup)
            org: Optional org identifier (for config fallback)
            
        Returns:
            Tuple of (is_in_scope, reason)
        """
        repo = repo or run.repo
        
        # Get configuration for this repo
        config = self.config_manager.get_config(repo, org)
        
        # Check 1: Number of files touched (configurable)
        files_touched = self._count_files_touched(run.diff)
        if files_touched > config.max_files_touched:
            return False, f"Touches {files_touched} files (max {config.max_files_touched} per config)"
        
        # Check 2: Number of lines changed (configurable)
        lines_changed = len(run.diff.split('\n'))
        if lines_changed > config.max_lines_changed:
            return False, f"Changes {lines_changed} lines (max {config.max_lines_changed} per config)"
        
        # Check 3: Protected files (HARDCODED, not configurable via numeric limits)
        is_protected, protected_file = self._touches_protected_files(run.diff, config)
        if is_protected:
            if config.allow_protected_paths_override:
                # Override is allowed, but must be logged
                return True, f"Protected file {protected_file} touched (override explicitly allowed: {config.protected_paths_override_reason})"
            else:
                return False, f"Modifies protected file: {protected_file} (override not enabled)"
        
        return True, "Patch is within safe scope"
    
    def _count_files_touched(self, diff: str) -> int:
        """Count unique files in diff"""
        files = set()
        for line in diff.split('\n'):
            if line.startswith('+++') or line.startswith('---'):
                # Extract filename
                parts = line.split('\t')
                if len(parts) > 0:
                    filename = parts[0][6:]  # Remove '--- a/' or '+++ b/'
                    files.add(filename)
        return len(files)
    
    def _touches_protected_files(self, diff: str, config: ScopeConfig) -> Tuple[bool, str]:
        """
        Check if diff modifies any protected files.
        
        Protected patterns are always enforced unless explicitly overridden.
        
        Args:
            diff: The diff to check
            config: ScopeConfig that may allow override
            
        Returns:
            Tuple of (is_protected, filename)
        """
        for pattern in self.PROTECTED_FILE_PATTERNS:
            for line in diff.split('\n'):
                if line.startswith('+++') or line.startswith('---'):
                    filename = line[6:]  # Remove prefix
                    if re.search(pattern, filename, re.IGNORECASE):
                        return True, filename
        return False, ""
    
    def create_scope_decision_step(self, run: Run, in_scope: bool, reason: str) -> Step:
        """
        Create a scope guard decision step.
        
        Args:
            run: The Run being evaluated
            in_scope: Whether in scope for auto-apply
            reason: Reason for decision
            
        Returns:
            Step object representing the decision
        """
        step = Step(
            type=StepType.VERIFICATION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS,
            input={
                'files_touched': self._count_files_touched(run.diff),
                'lines_changed': len(run.diff.split('\n')),
            },
            output={
                'in_scope': in_scope,
                'reasoning': reason,
            }
        )
        return step
