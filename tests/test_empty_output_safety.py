"""Safety fixes found in the first real-model tablib run (gpt-oss returned empty completions)."""

import subprocess

from src.context_buckets import checkout_code_context
from src.guardrails import ConfidenceGate
from src.model_provider import MockProvider, ModelResponse, completion_token_limit
from src.models import Attribution, Run, Step, StepType
from src.sandbox import RepoSandbox


class EmptyProvider(MockProvider):
    """Mimics a reasoning model that spent its whole budget thinking: success, no text."""

    def call(self, prompt, budget_tokens, temperature=0.7):
        return True, ModelResponse(content=None, input_tokens=10, output_tokens=300, stop_reason="length")


def test_empty_completion_is_a_failed_call():
    ok, resp = EmptyProvider(max_retries=0, base_wait_seconds=0).call_with_retry("p", 300)
    assert ok is False and "Empty completion" in resp.content and "length" in resp.content


def _run_with(attr):
    run = Run(repo="r", failing_commit="c")
    run.add_step(Step(type=StepType.ATTRIBUTION, attribution=attr))
    return run


def test_gate_refuses_when_there_is_no_supporting_evidence():
    attr = Attribution(claimed_cause="height() subtracts one", evidence_for=[], evidence_against=[],
                       counterfactual_result="pass", attribution_source="model")
    ok, reason = ConfidenceGate().is_eligible_for_auto_apply(_run_with(attr))
    assert ok is False and "No supporting evidence" in reason


def test_gate_refuses_placeholder_cause():
    attr = Attribution(claimed_cause="Model could not identify specific causes", evidence_for=["x"],
                       evidence_against=[], counterfactual_result="pass", attribution_source="model")
    ok, reason = ConfidenceGate().is_eligible_for_auto_apply(_run_with(attr))
    assert ok is False and "No usable claimed cause" in reason


def test_completion_token_limit_env(monkeypatch):
    assert completion_token_limit(300, 2000) == 300
    monkeypatch.setenv("KINTSUGI_MIN_COMPLETION_TOKENS", "4000")
    assert completion_token_limit(300, 2000) == 2000          # floor, still capped by provider
    monkeypatch.setenv("KINTSUGI_MAX_COMPLETION_TOKENS", "8000")
    assert completion_token_limit(300, 2000) == 4000


def test_checkout_code_context_shows_real_lines_with_numbers(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=tmp_path,
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (tmp_path / "m.py").write_text("".join(f"line{i}\n" for i in range(1, 21)))
    git("add", "-A"); git("commit", "-qm", "a")
    (tmp_path / "m.py").write_text("".join(("CHANGED\n" if i == 10 else f"line{i}\n") for i in range(1, 21)))
    git("commit", "-qam", "b")
    sha, diff = git("rev-parse", "HEAD"), git("diff", "HEAD~1", "HEAD")
    sb = RepoSandbox(repo_paths={"o/r": str(tmp_path)})
    ctx = checkout_code_context(sb, window=2)(Run(repo="o/r", failing_commit=sha, diff=diff))
    assert "m.py (lines" in ctx and "   10 | CHANGED" in ctx
    assert checkout_code_context(RepoSandbox(repo_paths={}))(Run(repo="o/r", failing_commit=sha, diff=diff)) is None
