"""Prompt templates for model-based repair handlers"""


class SemanticRepairPrompt:
    """Prompts for semantic (logic) failure repair"""
    
    @staticmethod
    def generate(
        failure_logs: str,
        diff: str,
        commit_message: str,
        attributed_cause: str,
        evidence_for: list,
        evidence_against: list,
    ) -> str:
        """
        Generate prompt for semantic repair.
        
        Args:
            failure_logs: The test failure output
            diff: The diff that was applied
            commit_message: The commit message
            attributed_cause: Root cause hypothesis
            evidence_for: Supporting evidence
            evidence_against: Contradicting evidence
            
        Returns:
            Prompt string for model
        """
        prompt = f"""You are a code repair expert analyzing a test failure.

**Failure Analysis:**
Attributed cause: {attributed_cause}

Supporting evidence:
{chr(10).join(f'- {e}' for e in evidence_for if e)}

Contradicting evidence:
{chr(10).join(f'- {e}' for e in evidence_against if e)}

**Test Failure Logs:**
{failure_logs}

**Recent Code Change (diff):**
{diff}

**Commit Message:**
{commit_message}

Based on this analysis, provide a specific code fix that addresses the root cause. Your response must include:

1. **Root Cause Analysis**: One sentence confirming why this failure occurred
2. **Proposed Fix**: The exact code change needed (in diff format or pseudocode)
3. **Why This Works**: One sentence explaining how this fix resolves the issue
4. **Risk Assessment**: What could go wrong with this fix? (be honest)

Format your response as:
ROOT_CAUSE: <one sentence>
PROPOSED_FIX: <code or diff>
WHY_THIS_WORKS: <one sentence>
RISK_ASSESSMENT: <honest assessment>
"""
        return prompt


class StructuralRepairPrompt:
    """Prompts for structural (format/config) failure repair"""
    
    @staticmethod
    def generate(
        failure_logs: str,
        diff: str,
        issue_type: str,
        schema_hint: str = "",
    ) -> str:
        """
        Generate prompt for structural repair.
        
        Args:
            failure_logs: The build failure output
            diff: The diff that was applied
            issue_type: Type of structural issue (json_error, yaml_error, lint_error, etc.)
            schema_hint: Optional hint about expected format
            
        Returns:
            Prompt string for model
        """
        prompt = f"""You are a configuration and code formatter expert.

**Issue Type:** {issue_type}

**Build Failure:**
{failure_logs}

**Recent Change (diff):**
{diff}

{"**Expected Format/Schema:** " + schema_hint if schema_hint else ""}

Provide a minimal, precise fix for this structural issue. Your response must include:

1. **Problem**: What is malformed?
2. **Solution**: The exact fix (in diff format)
3. **Verification**: How to verify this fix works

Format your response as:
PROBLEM: <what went wrong>
SOLUTION: <exact fix in diff format>
VERIFICATION: <how to test the fix>
"""
        return prompt


class AttributionPrompt:
    """Prompts for root cause attribution of semantic failures"""
    
    @staticmethod
    def generate(
        failure_logs: str,
        diff: str,
        commit_message: str,
    ) -> str:
        """
        Generate prompt for root cause attribution.
        
        Args:
            failure_logs: The test failure output
            diff: The code change
            commit_message: The commit message
            
        Returns:
            Prompt string for model
        """
        prompt = f"""Analyze this test failure and identify the root cause.

**Test Failure Output:**
{failure_logs}

**Code Change Applied (diff):**
{diff}

**Commit Message:**
{commit_message}

Identify the most likely root cause. Your response must include:

1. **Root Cause**: What specific change in the diff caused this test to fail?
2. **Why It Fails**: Explain the chain of events from the code change to the test failure
3. **Alternative Causes**: What else could cause this same failure? Why are they less likely?
4. **Confidence**: On a scale 0-1, how confident are you in this diagnosis?

Format your response as:
ROOT_CAUSE: <specific cause>
WHY_IT_FAILS: <chain of events>
ALTERNATIVES: <list other possibilities>
CONFIDENCE: <0-1 score>
"""
        return prompt


class CounterfactualPrompt:
    """Prompts for counterfactual verification"""
    
    @staticmethod
    def generate(
        attributed_cause: str,
        failure_logs: str,
        proposed_fix: str,
    ) -> str:
        """
        Generate prompt for counterfactual reasoning.
        
        Args:
            attributed_cause: The root cause hypothesis
            failure_logs: Original test failure
            proposed_fix: The proposed code fix
            
        Returns:
            Prompt string for model
        """
        prompt = f"""Perform a counterfactual analysis: if the proposed fix is applied, would the test pass?

**Attributed Root Cause:**
{attributed_cause}

**Original Failure:**
{failure_logs}

**Proposed Fix:**
{proposed_fix}

Reason through the counterfactual:
1. **If This Fix Applied**: Describe what the code would do differently
2. **Chain of Events**: Trace through the execution with the fix in place
3. **Test Result**: Would the test pass or still fail?
4. **Confidence**: How certain are you? (0-1 scale)

Format your response as:
IF_FIX_APPLIED: <what changes>
CHAIN_OF_EVENTS: <trace execution>
TEST_RESULT: pass | fail | inconclusive
CONFIDENCE: <0-1 score>
"""
        return prompt
