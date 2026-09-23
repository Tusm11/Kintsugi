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
2. **Proposed Fix**: The exact code change as a unified diff that `git apply` accepts (not pseudocode; it is applied and tested automatically)
3. **Why This Works**: One sentence explaining how this fix resolves the issue
4. **Risk Assessment**: What could go wrong with this fix? (be honest)

Format your response as:
ROOT_CAUSE: <one sentence>
PROPOSED_FIX:
```diff
<unified diff against the repository root, with --- a/<path> / +++ b/<path> headers and @@ hunk headers>
```
WHY_THIS_WORKS: <one sentence>
RISK_ASSESSMENT: <honest assessment>
"""
        return prompt

    @staticmethod
    def generate_from_bucket(bucket_text: str, bucket_tier: int, attempt_number: int, seeded: bool = False) -> str:
        """
        Generate a semantic repair prompt from a v2 context bucket.

        The bucket (see src/context_buckets.py) already holds the prioritized,
        size-bounded context for this attempt; this only wraps it with the
        instructions and the same response format as ``generate`` so response
        parsing is unchanged.

        Args:
            bucket_text: ContextBucket.render() output
            bucket_tier: 1, 2 or 3 (how much context this attempt gets)
            attempt_number: 1-based semantic attempt number
            seeded: True when the bucket carries a near-match seed attribution

        Returns:
            Prompt string for model
        """
        retry_note = ""
        if attempt_number > 1:
            retry_note = (
                f"\nThis is attempt {attempt_number}. A previous attempt for this failure did not "
                "produce a verified fix, so you have been given broader context.\n"
            )
        seed_note = ""
        if seeded:
            seed_note = (
                "\nA verified diagnosis of a *similar* past failure is included as a seed. "
                "Use it as a hint only; the current diagnosis and logs take precedence.\n"
            )
        return f"""You are a code repair expert analyzing a test failure.
{retry_note}{seed_note}
Context (bucket {bucket_tier} of 3; only what is below is available):

{bucket_text}

Based on this analysis, provide a specific code fix that addresses the root cause. Your response must include:

1. **Root Cause Analysis**: One sentence confirming why this failure occurred
2. **Proposed Fix**: The exact code change as a unified diff that `git apply` accepts (not pseudocode; it is applied and tested automatically)
3. **Why This Works**: One sentence explaining how this fix resolves the issue
4. **Risk Assessment**: What could go wrong with this fix? (be honest)

Format your response as:
ROOT_CAUSE: <one sentence>
PROPOSED_FIX:
```diff
<unified diff against the repository root, with --- a/<path> / +++ b/<path> headers and @@ hunk headers>
```
WHY_THIS_WORKS: <one sentence>
RISK_ASSESSMENT: <honest assessment>
"""


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
2. **Solution**: The exact fix as a unified diff that `git apply` accepts
3. **Verification**: How to verify this fix works

Format your response as:
PROBLEM: <what went wrong>
SOLUTION:
```diff
<unified diff against the repository root, with --- a/<path> / +++ b/<path> headers and @@ hunk headers>
```
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


# CounterfactualPrompt was removed: the counterfactual is now executed (revert the
# suspected hunks, re-run the failing tests) in AttributionEngine._test_counterfactual,
# not asked of a model.
