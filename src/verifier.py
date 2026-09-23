"""Verifier: runs the repo's real test suite against the proposed fix"""

from typing import Tuple, Optional, Dict, Any
from src.models import Run, Step, StepType, StepLayer, StepStatus, Cost
from src.sandbox import RepoSandbox


class Verifier:
    """
    The ONLY component in the system permitted to mark a Step as `success`.

    No handler, model, or confidence score can self-certify.
    Only the Verifier can claim that a fix actually worked.

    How it verifies (see src/sandbox.py):
      1. Take the patch from the most recent REPAIR step (`repair_output.fix_patch`).
      2. Create a throwaway git worktree of the repo's configured local
         checkout at the Run's failing commit.
      3. `git apply` the patch. A patch that does not apply is a FAIL.
      4. Run the configured test command (KINTSUGI_TEST_COMMAND, default
         `python -m pytest -q --tb=short`). Exit code 0 is a PASS; anything
         else (including a timeout) is a FAIL.

    Mechanical repairs carry no patch: their "fix" is a retry, so the suite is
    re-run unchanged at the failing commit.

    It never passes by default. No configured checkout, no patch, a patch that
    doesn't apply, a missing test runner: all FAIL with the reason recorded.
    """

    def __init__(
        self,
        sandbox_cmd: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        sandbox: Optional[RepoSandbox] = None,
    ):
        """
        Initialize verifier.

        Args:
            sandbox_cmd: Test command override (e.g. "pytest -q", "npm test").
                Ignored when `sandbox` is passed.
            timeout_ms: Test timeout override in milliseconds. Ignored when
                `sandbox` is passed.
            sandbox: Shared RepoSandbox (the pipeline passes one so the
                Verifier, counterfactual and Action Layer use the same config).
        """
        self.sandbox = sandbox or RepoSandbox(
            test_command=sandbox_cmd,
            timeout_seconds=(timeout_ms / 1000.0) if timeout_ms else None,
        )
        self.sandbox_cmd = self.sandbox.test_command
        self.timeout_ms = int(self.sandbox.timeout_seconds * 1000)

    def verify(self, run: Run) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Apply the proposed fix in a sandbox worktree and run the test suite.

        Args:
            run: The Run with proposed fix to verify

        Returns:
            Tuple of (pass, reason, output). `output` carries the test counts,
            exit code, timing and the tail of the test output.
        """
        # AX-VERIFIED: verify() returns pass only when the test command exited 0
        # after the patch applied; every setup problem returns False.
        # (tests/test_sandbox.py::TestVerifier)
        repair_step = self._get_last_repair_step(run)
        if not repair_step:
            return False, "No repair to verify", {}

        repair_output = repair_step.output.get('repair_output') or {}
        patch = repair_output.get('fix_patch')
        if repair_step.layer == StepLayer.MECHANICAL:
            patch = None  # a mechanical "fix" is a retry: re-run the suite unchanged
        elif not (patch or "").strip():
            return False, "Repair produced no patch to verify", {'patch_applied': False}

        if not self.sandbox.is_configured(run.repo):
            return False, self.sandbox.not_configured_reason(run.repo), {'patch_applied': False}

        result = self.sandbox.run_with_patch(run.repo, run.failing_commit, patch)
        if not result.applied:
            return False, f"Patch did not apply: {result.apply_error}", {
                'patch_applied': False, 'apply_error': result.apply_error,
            }

        output = result.tests.to_dict()
        output['patch_applied'] = patch is not None
        output['verified_commit'] = result.head_commit
        if result.tests.timed_out:
            return False, f"Tests timed out after {self.sandbox.timeout_seconds:.0f}s", output
        if result.passed:
            return True, "All tests passed", output
        return False, (
            f"Tests failed (exit {result.tests.returncode}: "
            f"{output['tests_failed']} failed, {output['tests_errored']} errors)"
        ), output

    def _get_last_repair_step(self, run: Run) -> Optional[Step]:
        """Get the most recent repair step"""
        for step in reversed(run.steps):
            if step.type == StepType.REPAIR:
                return step
        return None

    def create_verification_step(self, run: Run, passed: bool, reason: str, output: Dict[str, Any]) -> Step:
        """
        Create verification step to add to run.

        Args:
            run: The Run being verified
            passed: Whether verification passed
            reason: Reason/summary
            output: Test output data

        Returns:
            Step object representing verification
        """
        step = Step(
            type=StepType.VERIFICATION,
            layer=StepLayer.SEMANTIC,
            status=StepStatus.SUCCESS if passed else StepStatus.FAILED,
            input={
                'repair_present': self._get_last_repair_step(run) is not None,
                'test_command': self.sandbox_cmd,
            },
            output={
                'passed': passed,
                'reason': reason,
                'test_results': output,
            },
            cost=Cost(tokens_used=0, wall_clock_ms=int(output.get('execution_time_ms', 0)))
        )
        return step

    def verify_and_record(self, run: Run) -> bool:
        """
        Verify fix and add verification step to run.

        Args:
            run: The Run to verify

        Returns:
            Whether verification passed
        """
        passed, reason, output = self.verify(run)
        step = self.create_verification_step(run, passed, reason, output)
        run.add_step(step)
        return passed
