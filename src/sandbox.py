"""Repo Sandbox: apply patches and run a repo's real test suite in a throwaway git worktree.

This replaces every place v1 *simulated* running code:

  * Verifier      -> apply the proposed patch at the failing commit, run tests
  * Counterfactual-> reverse-apply the suspected cause's hunks, run the failing tests
  * Action Layer  -> re-apply the verified patch to collect the changed files for the PR

Where the code comes from
-------------------------
From a **local checkout** you configure per repo (no cloning, no network):

    KINTSUGI_REPO_PATH_<REPO>   e.g. KINTSUGI_REPO_PATH_USER_REPO=D:\\code\\repo  for "user/repo"
    KINTSUGI_REPO_PATHS         JSON map, e.g. {"user/repo": "D:\\\\code\\\\repo"}

<REPO> is the repo name upper-cased with every non-alphanumeric character
replaced by "_". The checkout must already contain the failing commit
(``git fetch`` it first); the sandbox never fetches.

For each operation a detached ``git worktree`` is created at the failing commit
in a temp directory, used, and removed. Your checkout's working tree, index and
branches are never touched.

What this is NOT
----------------
It is not a security sandbox. The repo's tests run as your user, on your
machine, with filesystem and network access. Two mitigations only:

  * a scrubbed environment: only PATH/HOME/temp/locale-type variables are
    passed to the test process, so API keys and GITHUB_TOKEN in Kintsugi's own
    environment are NOT visible to the code under test
    (add more with KINTSUGI_TEST_ENV_PASSTHROUGH="VAR1,VAR2");
  * a timeout (KINTSUGI_TEST_TIMEOUT_SECONDS, default 600).

Run Kintsugi only against repos whose tests you would run yourself.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union


DEFAULT_TEST_COMMAND = "python -m pytest -q --tb=short"
DEFAULT_TIMEOUT_SECONDS = 600
OUTPUT_TAIL_CHARS = 4000

# Environment variables passed through to the test process. Everything else —
# notably *_API_KEY and GITHUB_TOKEN — is withheld from code under test.
_BASE_ENV_PASSTHROUGH = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONIOENCODING", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)

_FENCE_RE = re.compile(r"^\s*```[\w+-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)
_FAILED_TEST_RE = re.compile(r"^\s*(?:FAILED|ERROR)\s+(\S+::\S+)", re.MULTILINE)
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)")


class SandboxError(RuntimeError):
    """A git/worktree operation failed (not a test failure)."""


def repo_env_key(repo: str) -> str:
    """Env var name holding the local checkout path for `repo`."""
    return "KINTSUGI_REPO_PATH_" + re.sub(r"[^A-Za-z0-9]", "_", repo).upper()


def normalize_patch(patch: str) -> str:
    """Strip a surrounding ``` fence and guarantee a trailing newline (git apply needs it)."""
    text = (patch or "").strip("\n")
    match = _FENCE_RE.match(text)
    if match:
        text = match.group(1)
    return text.rstrip("\n") + "\n" if text.strip() else ""


def failing_test_ids(logs: str) -> List[str]:
    """pytest node ids from `FAILED path::test` / `ERROR path::test` lines (parametrize ids kept)."""
    seen: List[str] = []
    for test_id in _FAILED_TEST_RE.findall(logs or ""):
        if test_id not in seen:
            seen.append(test_id)
    return seen


@dataclass
class SuiteRunResult:
    passed: bool
    returncode: Optional[int]
    command: List[str]
    duration_ms: int
    timed_out: bool = False
    counts: Dict[str, int] = field(default_factory=dict)
    output_tail: str = ""

    def to_dict(self) -> Dict[str, object]:
        failed = self.counts.get("failed", 0)
        errors = self.counts.get("errors", 0) + self.counts.get("error", 0)
        passed = self.counts.get("passed", 0)
        return {
            "tests_run": passed + failed + errors,
            "tests_passed": passed,
            "tests_failed": failed,
            "tests_errored": errors,
            "execution_time_ms": self.duration_ms,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "test_command": " ".join(self.command),
            "output_tail": self.output_tail,
        }


@dataclass
class PatchRunResult:
    """Outcome of apply-then-test. `applied=False` means tests never ran."""
    applied: bool
    apply_error: str = ""
    tests: Optional[SuiteRunResult] = None
    head_commit: str = ""

    @property
    def passed(self) -> bool:
        return self.applied and self.tests is not None and self.tests.passed


@dataclass
class ChangedFile:
    path: str
    mode: str                # git file mode, e.g. "100644"
    content: Optional[bytes]  # None = deleted


class RepoSandbox:
    """Throwaway-worktree runner over a configured local checkout. See module docstring."""

    def __init__(
        self,
        repo_paths: Optional[Dict[str, str]] = None,
        test_command: Optional[Union[str, List[str]]] = None,
        timeout_seconds: Optional[float] = None,
        env_passthrough: Optional[List[str]] = None,
        git_bin: str = "git",
    ):
        self._explicit_paths = dict(repo_paths or {})
        cmd = test_command or os.getenv("KINTSUGI_TEST_COMMAND") or DEFAULT_TEST_COMMAND
        # A list is used verbatim (safest on Windows, where quoted paths with
        # spaces do not survive string splitting); a string is shlex-split.
        self._test_argv: List[str] = list(cmd) if isinstance(cmd, (list, tuple)) else shlex.split(cmd, posix=(os.name != "nt"))
        self.test_command = cmd if isinstance(cmd, str) else subprocess.list2cmdline(self._test_argv)
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None
            else os.getenv("KINTSUGI_TEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        )
        extra = env_passthrough
        if extra is None:
            extra = [v.strip() for v in os.getenv("KINTSUGI_TEST_ENV_PASSTHROUGH", "").split(",") if v.strip()]
        self.env_passthrough = tuple(_BASE_ENV_PASSTHROUGH) + tuple(extra)
        self.git_bin = git_bin

    # -- configuration --------------------------------------------------

    def checkout_path(self, repo: str) -> Optional[str]:
        """Resolve the local checkout for `repo`: explicit map > per-repo env var > JSON env map."""
        if repo in self._explicit_paths:
            return self._explicit_paths[repo]
        direct = os.getenv(repo_env_key(repo))
        if direct:
            return direct
        raw = os.getenv("KINTSUGI_REPO_PATHS")
        if raw:
            try:
                mapping = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if isinstance(mapping, dict) and mapping.get(repo):
                return str(mapping[repo])
        return None

    def is_configured(self, repo: str) -> bool:
        path = self.checkout_path(repo)
        return bool(path) and Path(path, ".git").exists()

    def not_configured_reason(self, repo: str) -> str:
        path = self.checkout_path(repo)
        if not path:
            return f"No local checkout configured for {repo} (set {repo_env_key(repo)} or KINTSUGI_REPO_PATHS)"
        return f"Configured checkout for {repo} is not a git repository: {path}"

    # -- git plumbing ---------------------------------------------------

    def _git(self, cwd: str, *args: str, check: bool = True, input_bytes: Optional[bytes] = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self.git_bin, *args], cwd=cwd, input=input_bytes,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
        )
        if check and proc.returncode != 0:
            raise SandboxError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc

    @contextmanager
    def worktree(self, repo: str, commit: str) -> Iterator[Path]:
        """Detached worktree of `repo` at `commit` in a temp dir; always removed afterwards."""
        # AX-VERIFIED: the user's checkout (working tree, index, HEAD) is unchanged
        # after any sandbox operation, including a failed one.
        # (tests/test_sandbox.py::TestWorktree::test_checkout_untouched_after_run,
        # ::test_worktree_removed_even_on_error)
        source = self.checkout_path(repo)
        if not source or not Path(source, ".git").exists():
            raise SandboxError(self.not_configured_reason(repo))
        if not commit:
            raise SandboxError("No failing commit on the Run; cannot create a worktree")
        tmp_root = tempfile.mkdtemp(prefix="kintsugi-wt-")
        workdir = Path(tmp_root) / "repo"
        self._git(source, "worktree", "add", "--detach", str(workdir), commit)
        try:
            yield workdir
        finally:
            self._git(source, "worktree", "remove", "--force", str(workdir), check=False)
            shutil.rmtree(tmp_root, ignore_errors=True)
            self._git(source, "worktree", "prune", check=False)

    def apply_patch(self, workdir: Path, patch: str, reverse: bool = False) -> Tuple[bool, str]:
        """`git apply` (optionally -R) a unified diff. Checks first so a bad patch changes nothing.
        
        Tried strict first, then with --unidiff-zero. Model-written hunks often
        have lopsided context (e.g. one leading line, no trailing lines), which
        strict git apply anchors to the start/end of the file and rejects.
        --unidiff-zero lifts only that anchoring: every context and removed
        line must still match the file exactly.
        """
        text = normalize_patch(patch)
        if not text:
            return False, "empty patch"
        data = text.encode("utf-8")
        base = ["apply", "--whitespace=nowarn", "--recount"] + (["-R"] if reverse else [])
        errors = []
        for extra in ([], ["--unidiff-zero"]):
            args = base + extra
            check = self._git(str(workdir), *args, "--check", "-", check=False, input_bytes=data)
            if check.returncode == 0:
                self._git(str(workdir), *args, "-", input_bytes=data)
                return True, ""
            errors.append(check.stderr.decode(errors="replace").strip())
        return False, errors[0] or "git apply --check failed"
    
    def head_commit(self, workdir: Path) -> str:
        return self._git(str(workdir), "rev-parse", "HEAD").stdout.decode().strip()

    # -- tests ------------------------------------------------------------

    def _test_env(self) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k.upper() in {p.upper() for p in self.env_passthrough}}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def run_tests(self, workdir: Path, test_ids: Optional[List[str]] = None) -> SuiteRunResult:
        """Run the configured test command (plus optional node ids) in `workdir`."""
        command = list(self._test_argv) + list(test_ids or [])
        start = time.monotonic()
        try:
            proc = subprocess.run(
                command, cwd=str(workdir), env=self._test_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode(errors="replace")
            return SuiteRunResult(False, None, command, int((time.monotonic() - start) * 1000), True,
                                 _parse_counts(out), out[-OUTPUT_TAIL_CHARS:])
        except FileNotFoundError as exc:
            return SuiteRunResult(False, None, command, 0, False, {}, f"test command not found: {exc}")
        out = proc.stdout.decode(errors="replace")
        return SuiteRunResult(
            passed=proc.returncode == 0,
            returncode=proc.returncode,
            command=command,
            duration_ms=int((time.monotonic() - start) * 1000),
            counts=_parse_counts(out),
            output_tail=out[-OUTPUT_TAIL_CHARS:],
        )

    # -- composite operations ------------------------------------------

    def run_with_patch(
        self, repo: str, commit: str, patch: Optional[str],
        reverse: bool = False, test_ids: Optional[List[str]] = None,
    ) -> PatchRunResult:
        """Worktree at `commit` -> apply `patch` (skipped if None) -> run tests -> clean up."""
        try:
            with self.worktree(repo, commit) as wt:
                head = self.head_commit(wt)
                if patch is not None:
                    ok, err = self.apply_patch(wt, patch, reverse=reverse)
                    if not ok:
                        return PatchRunResult(False, err, None, head)
                return PatchRunResult(True, "", self.run_tests(wt, test_ids), head)
        except SandboxError as exc:
            return PatchRunResult(False, str(exc))

    def collect_patched_files(self, repo: str, commit: str, patch: str) -> Tuple[str, List[ChangedFile]]:
        """Apply `patch` at `commit` and return (full head sha, changed files with contents)."""
        with self.worktree(repo, commit) as wt:
            head = self.head_commit(wt)
            ok, err = self.apply_patch(wt, patch)
            if not ok:
                raise SandboxError(f"patch did not apply: {err}")
            self._git(str(wt), "add", "-A")
            status = self._git(str(wt), "diff", "--cached", "--name-status", "--no-renames", "-z").stdout.decode()
            parts = [p for p in status.split("\0") if p]
            changes: List[ChangedFile] = []
            for code, path in zip(parts[0::2], parts[1::2]):
                if code.startswith("D"):
                    old = self._git(str(wt), "ls-tree", "HEAD", "--", path).stdout.decode().split()
                    changes.append(ChangedFile(path, old[0] if old else "100644", None))
                    continue
                stage = self._git(str(wt), "ls-files", "-s", "--", path).stdout.decode().split()
                mode = stage[0] if stage else "100644"
                changes.append(ChangedFile(path, mode, (wt / path).read_bytes()))
            return head, changes


def _parse_counts(output: str) -> Dict[str, int]:
    """Counts from the last pytest-style summary line ("3 passed, 1 failed in 0.2s")."""
    counts: Dict[str, int] = {}
    for line in reversed(output.splitlines()):
        found = _COUNT_RE.findall(line)
        if found:
            for n, kind in found:
                counts[kind] = int(n)
            break
    return counts
