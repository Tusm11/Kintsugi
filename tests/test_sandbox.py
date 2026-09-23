"""Tests for the real (non-simulated) execution path: sandbox, Verifier, counterfactual, Action Layer.

Each test builds a tiny throwaway git repo:

    commit 1 (good):   calc.py -> return 42, test_calc.py asserts 42, test_other.py passes
    commit 2 (broken): calc.py -> return 41   <- the "failing commit"

and runs the repo's tests with the current interpreter's pytest. GitHub is
never contacted: the Action Layer gets a fake transport that records requests.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.action_layer import ActionLayer
from src.attribution import AttributionEngine, select_cause_hunks
from src.github_client import GitHubClient
from src.models import Attribution, Run, Step, StepLayer, StepType
from src.sandbox import RepoSandbox, failing_test_ids, normalize_patch, repo_env_key
from src.verifier import Verifier

REPO = "someone/calc"
TEST_CMD = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
FAIL_LOG = "FAILED test_calc.py::test_total - AssertionError: assert 41 == 42"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "calc"
    root.mkdir()
    git(root, "init", "-q")
    (root / "calc.py").write_text("def total():\n    return 42\n\n\ndef other():\n    return 'ok'\n")
    (root / "test_calc.py").write_text("from calc import total\n\n\ndef test_total():\n    assert total() == 42\n")
    (root / "test_other.py").write_text("from calc import other\n\n\ndef test_other():\n    assert other() == 'ok'\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "good")
    (root / "calc.py").write_text("def total():\n    return 41\n\n\ndef other():\n    return 'ok'\n")
    git(root, "commit", "-q", "-am", "break total")
    return {
        "path": root,
        "failing": git(root, "rev-parse", "HEAD"),
        "diff": git(root, "diff", "HEAD~1", "HEAD") + "\n",
    }


@pytest.fixture
def sandbox(repo):
    return RepoSandbox(repo_paths={REPO: str(repo["path"])}, test_command=TEST_CMD, timeout_seconds=120)


FIX_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def total():
-    return 41
+    return 42
"""
WRONG_PATCH = FIX_PATCH.replace("+    return 42", "+    return 40")
BAD_PATCH = FIX_PATCH.replace(" def total():", " def nope():")


def run_with_repair(repo, patch, layer=StepLayer.SEMANTIC):
    run = Run(repo=REPO, failing_commit=repo["failing"], failure_logs=FAIL_LOG, diff=repo["diff"])
    run.add_step(Step(type=StepType.REPAIR, layer=layer, output={"repair_output": {"fix_patch": patch}}))
    return run


class TestWorktree:
    def test_checkout_untouched_after_run(self, repo, sandbox):
        before = (git(repo["path"], "rev-parse", "HEAD"), git(repo["path"], "status", "--porcelain"))
        sandbox.run_with_patch(REPO, repo["failing"], FIX_PATCH)
        after = (git(repo["path"], "rev-parse", "HEAD"), git(repo["path"], "status", "--porcelain"))
        assert before == after
        assert "return 41" in (repo["path"] / "calc.py").read_text()

    def test_worktree_removed_even_on_error(self, repo, sandbox):
        with pytest.raises(RuntimeError):
            with sandbox.worktree(REPO, repo["failing"]) as wt:
                assert wt.exists()
                raise RuntimeError("boom")
        assert len(git(repo["path"], "worktree", "list").splitlines()) == 1

    def test_env_is_scrubbed(self, sandbox, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "secret")
        monkeypatch.setenv("GITHUB_TOKEN", "secret")
        env = sandbox._test_env()
        assert "GROQ_API_KEY" not in env and "GITHUB_TOKEN" not in env
        assert "PATH" in env or "Path" in env

    def test_config_from_env(self, repo, monkeypatch):
        monkeypatch.setenv(repo_env_key(REPO), str(repo["path"]))
        assert RepoSandbox().is_configured(REPO)
        monkeypatch.delenv(repo_env_key(REPO))
        monkeypatch.setenv("KINTSUGI_REPO_PATHS", json.dumps({REPO: str(repo["path"])}))
        assert RepoSandbox().is_configured(REPO)


class TestVerifier:
    def test_correct_patch_passes(self, repo, sandbox):
        passed, reason, out = Verifier(sandbox=sandbox).verify(run_with_repair(repo, FIX_PATCH))
        assert passed, reason
        assert out["tests_passed"] == 2 and out["returncode"] == 0

    def test_fenced_patch_from_model_still_applies(self, repo, sandbox):
        fenced = "```diff\n" + FIX_PATCH + "```"
        assert Verifier(sandbox=sandbox).verify(run_with_repair(repo, fenced))[0]

    def test_wrong_patch_fails_on_tests(self, repo, sandbox):
        passed, reason, out = Verifier(sandbox=sandbox).verify(run_with_repair(repo, WRONG_PATCH))
        assert not passed and "Tests failed" in reason
        assert out["tests_failed"] == 1

    def test_non_applying_patch_fails(self, repo, sandbox):
        passed, reason, _ = Verifier(sandbox=sandbox).verify(run_with_repair(repo, BAD_PATCH))
        assert not passed and reason.startswith("Patch did not apply")

    def test_pseudocode_is_rejected(self, repo, sandbox):
        passed, reason, _ = Verifier(sandbox=sandbox).verify(run_with_repair(repo, "change 41 to 42 in total()"))
        assert not passed and reason.startswith("Patch did not apply")

    def test_no_checkout_configured_fails(self, repo):
        passed, reason, _ = Verifier(sandbox=RepoSandbox(repo_paths={})).verify(run_with_repair(repo, FIX_PATCH))
        assert not passed and "No local checkout configured" in reason

    def test_no_patch_fails(self, repo, sandbox):
        passed, reason, _ = Verifier(sandbox=sandbox).verify(run_with_repair(repo, ""))
        assert not passed and "no patch" in reason

    def test_mechanical_retry_reruns_unchanged(self, repo, sandbox):
        # A retry of a genuinely broken commit must fail: nothing is patched.
        passed, reason, out = Verifier(sandbox=sandbox).verify(run_with_repair(repo, None, StepLayer.MECHANICAL))
        assert not passed and out["patch_applied"] is False

    def test_missing_test_runner_fails(self, repo):
        sb = RepoSandbox(repo_paths={REPO: str(repo["path"])}, test_command=["definitely-not-a-real-binary-xyz"])
        passed, _, out = Verifier(sandbox=sb).verify(run_with_repair(repo, FIX_PATCH))
        assert not passed and out["returncode"] is None


class TestCounterfactual:
    def test_reverting_the_cause_passes(self, repo, sandbox):
        engine = AttributionEngine(sandbox=sandbox)
        run = Run(repo=REPO, failing_commit=repo["failing"], failure_logs=FAIL_LOG, diff=repo["diff"])
        result, source = engine._test_counterfactual("total() in calc.py returns 41 instead of 42", repo["diff"], run)
        assert (result.counterfactual_outcome, source) == ("pass", "execution")

    def test_not_configured_is_inconclusive(self, repo):
        engine = AttributionEngine(sandbox=RepoSandbox(repo_paths={}))
        run = Run(repo=REPO, failing_commit=repo["failing"], failure_logs=FAIL_LOG, diff=repo["diff"])
        result, source = engine._test_counterfactual("calc.py total", repo["diff"], run)
        assert (result.counterfactual_outcome, source) == ("inconclusive", "not_executed")

    def test_unrelated_hunk_still_fails(self, repo, sandbox):
        # Add a second, harmless change and blame it: reverting it must NOT fix the test.
        root = repo["path"]
        (root / "util.py").write_text("VERSION = 2\n")
        git(root, "add", "util.py")
        git(root, "commit", "-q", "-m", "second, harmless change")
        diff = git(root, "diff", "HEAD~2", "HEAD") + "\n"
        run = Run(repo=REPO, failing_commit=git(root, "rev-parse", "HEAD"), failure_logs=FAIL_LOG, diff=diff)
        engine = AttributionEngine(sandbox=sandbox)
        cause = "VERSION bumped in util.py"
        patch, _ = select_cause_hunks(cause, [], diff)
        assert patch is not None and "util.py" in patch and "calc.py" not in patch
        result, source = engine._test_counterfactual(cause, diff, run)
        assert (result.counterfactual_outcome, source) == ("fail", "execution")


class TestHunkSelection:
    DIFF = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x = compute_price()\n+x = compute_cost()\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-y = 1\n+y = 2\n"
    )

    def test_selects_by_identifier(self):
        patch, _ = select_cause_hunks("compute_cost is called instead of compute_price", [], self.DIFF)
        assert "a.py" in patch and "b.py" not in patch

    def test_selects_by_file(self):
        patch, _ = select_cause_hunks("constant changed in b.py", [], self.DIFF)
        assert "b.py" in patch and "a.py" not in patch

    def test_refuses_to_guess_between_multiple_hunks(self):
        patch, note = select_cause_hunks("something vague", [], self.DIFF)
        assert patch is None and "could not isolate" in note

    def test_no_hunks(self):
        assert select_cause_hunks("x", [], "- return 42\n+ return 41")[0] is None


def test_helpers():
    assert normalize_patch("```diff\n--- a/x\n+++ b/x\n```") == "--- a/x\n+++ b/x\n"
    assert failing_test_ids("FAILED t.py::a - x\nFAILED t.py::a - y\nERROR t.py::b[1]") == ["t.py::a", "t.py::b[1]"]


# ---------------------------------------------------------------------------
# Action Layer against a fake GitHub transport
# ---------------------------------------------------------------------------

class FakeGitHub:
    """Records requests; answers the Git Data / Pulls / Issues endpoints the Action Layer uses."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        path = req.full_url.split("api.github.test", 1)[1]
        self.calls.append((req.get_method(), path, body, req.headers.get("Authorization")))
        if "/git/commits/" in path and req.get_method() == "GET":
            payload = {"tree": {"sha": "basetree"}}
        elif path.endswith("/git/blobs"):
            payload = {"sha": f"blob{len(self.calls)}"}
        elif path.endswith("/git/trees"):
            payload = {"sha": "newtree"}
        elif path.endswith("/git/commits"):
            payload = {"sha": "fixcommit"}
        elif path.endswith("/git/refs"):
            payload = {"ref": body["ref"]}
        elif path.endswith("/pulls"):
            payload = {"html_url": "https://github.test/someone/calc/pull/7"}
        elif path.endswith("/issues"):
            payload = {"html_url": "https://github.test/someone/calc/issues/3"}
        else:
            payload = {}
        return io.BytesIO(json.dumps(payload).encode())


def approved_run(repo, patch=FIX_PATCH):
    run = run_with_repair(repo, patch)
    run.metadata["branch"] = "develop"
    run.add_step(Step(type=StepType.VERIFICATION, output={"passed": True, "reason": "All tests passed", "test_results": {}}))
    run.add_step(Step(type=StepType.VERIFICATION, output={"auto_apply_eligible": True}))
    run.add_step(Step(type=StepType.VERIFICATION, output={"in_scope": True}))
    return run


class TestActionLayer:
    def test_opens_real_pr_via_git_data_api(self, repo, sandbox):
        fake = FakeGitHub()
        layer = ActionLayer(github=GitHubClient(token="tok", api_url="https://api.github.test", opener=fake), sandbox=sandbox)
        ok, url = layer.apply_fix(approved_run(repo))
        assert ok and url.endswith("/pull/7")
        methods_paths = [(m, p.split("/", 4)[-1]) for m, p, _, _ in fake.calls]
        assert [mp[0] for mp in methods_paths] == ["GET", "POST", "POST", "POST", "POST", "POST"]
        tree_body = next(b for m, p, b, _ in fake.calls if p.endswith("/git/trees"))
        assert tree_body["base_tree"] == "basetree" and tree_body["tree"][0]["path"] == "calc.py"
        commit_body = next(b for m, p, b, _ in fake.calls if p.endswith("/git/commits") and m == "POST")
        assert commit_body["parents"] == [repo["failing"]]
        pr_body = next(b for m, p, b, _ in fake.calls if p.endswith("/pulls"))
        assert pr_body["base"] == "develop" and pr_body["head"].startswith("kintsugi/fix-")
        assert all(auth == "Bearer tok" for *_, auth in fake.calls)

    def test_no_token_means_no_pr_and_no_fake_url(self, repo, sandbox):
        layer = ActionLayer(github=GitHubClient(token=""), sandbox=sandbox)
        ok, msg = layer.apply_fix(approved_run(repo))
        assert not ok and "GITHUB_TOKEN" in msg and "http" not in msg

    def test_escalation_opens_issue(self, repo, sandbox):
        fake = FakeGitHub()
        layer = ActionLayer(github=GitHubClient(token="tok", api_url="https://api.github.test", opener=fake), sandbox=sandbox)
        ok, url = layer.escalate(run_with_repair(repo, FIX_PATCH), "Confidence gate")
        assert ok and url.endswith("/issues/3")
        assert "Confidence gate" in fake.calls[0][2]["body"]

    def test_escalation_without_token_is_recorded_not_faked(self, repo, sandbox):
        layer = ActionLayer(github=GitHubClient(token=""), sandbox=sandbox)
        ok, msg = layer.escalate(run_with_repair(repo, FIX_PATCH), "x")
        assert not ok and "http" not in msg
        assert layer.get_actions_taken()[0]["status"] == "not_posted"
