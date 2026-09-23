# Real Execution: Sandbox, Verifier, Counterfactual, Action Layer

`src/sandbox.py`, `src/verifier.py`, `src/attribution.py` (`_test_counterfactual`, `select_cause_hunks`), `src/action_layer.py`, `src/github_client.py` · tests: `tests/test_sandbox.py`

## What changed

v1 simulated every step that should have touched real code. v2 replaces each of them:

| Component | v1 behaviour | Now |
|---|---|---|
| Verifier | Always returned "10/10 tests passed" without running anything | Applies the patch in a throwaway git worktree and runs the repo's real test command. Exit code 0 is the only pass. |
| Counterfactual | A model guessed from keywords ("would pass", "fix") | Reverts the suspected cause's hunks at the failing commit and re-runs the failing tests |
| Action Layer: PR | Returned `https://github.com/<repo>/pull/12345` | Opens a real PR through the GitHub REST API (Git Data API: blobs → tree → commit → branch → pull) |
| Action Layer: escalation | Returned `…/issues/54321` | Opens a real GitHub issue with the trace. Without a token it records the escalation locally and says so. |
| v1 Output Guardrail | Scanned the constant `"def fix(): return True"` | Scans the structural patch, or the recorded action text for a mechanical retry |
| Handler defaults | Fell back to `MockProvider` when no provider was passed | Raise `ValueError`. Tests pass a `MockProvider` explicitly. |
| Mechanical "repairs" | Claimed "timeout increased to 120s" / "exponential backoff" and did neither | Every strategy is described as what really happens: a retry, where the Verifier re-runs the suite unchanged |
| Prompts | Accepted "diff format or pseudocode" | Require a unified diff that `git apply` accepts, because the patch is applied for real |

## Configuration

| Variable | Purpose |
|---|---|
| `KINTSUGI_REPO_PATH_<REPO>` | Local checkout for one repo. `<REPO>` is the repo name upper-cased, with every non-alphanumeric character replaced by `_`. Example: `KINTSUGI_REPO_PATH_OWNER_PROJECT=D:\code\project` for `owner/project`. |
| `KINTSUGI_REPO_PATHS` | Alternative: a JSON map `{"owner/repo": "path", ...}` |
| `KINTSUGI_TEST_COMMAND` | Default `python -m pytest -q --tb=short`. In code you can pass a list, which is safer on Windows when paths contain spaces. |
| `KINTSUGI_TEST_TIMEOUT_SECONDS` | Default 600 |
| `KINTSUGI_TEST_ENV_PASSTHROUGH` | Comma-separated extra env vars that the code under test may see |
| `GITHUB_TOKEN` | Token with contents, pull-requests and issues write access on the target repos |
| `GITHUB_API_URL` | Default `https://api.github.com`. Change it for GitHub Enterprise. |
| `KINTSUGI_DEFAULT_BASE_BRANCH` | PR base when the Run carries no branch. Default `main`. |

The checkout must already contain the failing commit (`git fetch` first). The sandbox never fetches, clones or pushes.

## Sandbox (`RepoSandbox`)

Every operation follows the same pattern:

```
git -C <checkout> worktree add --detach <tmp>/repo <failing_commit>
  … apply patch / run tests / read files …
git worktree remove --force <tmp>/repo; delete <tmp>; git worktree prune
```

- **Your checkout's working tree, index, HEAD and branches are never touched,** including when an operation fails. Tests: `TestWorktree::test_checkout_untouched_after_run` and `::test_worktree_removed_even_on_error`.
- **Patches are applied check-first.** `git apply --check` runs before `git apply`, so a bad patch changes nothing. Model output wrapped in a ```` ```diff ```` fence is unwrapped automatically.
- **Strict mode is tried first, then `--unidiff-zero`.** Models often write hunks with lopsided context, for example one leading line and no trailing lines. Strict `git apply` pins such a hunk to the start or end of the file and rejects it anywhere else. `--unidiff-zero` lifts only that anchoring; every context line and removed line must still match the file exactly.
- **The test environment is scrubbed.** Only PATH, HOME, temp, locale and Python-path variables are passed to the test process. `GROQ_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN` and the rest of Kintsugi's environment are withheld from the code under test.

> **This is not a security sandbox.** The repo's tests run as you, on your machine, with filesystem and network access. The only protections are the scrubbed environment and the timeout. Run Kintsugi only against repos whose tests you would run yourself. A container-based runner would be the next step if that ever changes.

## Verifier

1. Take `repair_output.fix_patch` from the latest REPAIR step.
2. **Mechanical** step: there is no patch, so re-run the suite unchanged at the failing commit (that is what a retry is).
3. **Structural/semantic** step with no patch → FAIL: "Repair produced no patch to verify".
4. No configured checkout → FAIL, naming the exact env var to set.
5. Patch does not apply → FAIL: "Patch did not apply: …". This is also how pseudocode is rejected.
6. Run the tests. Exit 0 → PASS. Non-zero exit, a timeout, or a missing test runner → FAIL.

`test_results` records tests run/passed/failed/errored (parsed from the pytest summary line), the exit code, the duration, the command, the last 4,000 characters of output, and the full commit sha that was verified.

**The Verifier never passes by default.** Every setup problem is a recorded failure. This is what makes the fix-cache's "busted on failed re-verification" path real rather than test-only.

## Counterfactual (`AttributionEngine._test_counterfactual`)

The README always described this step as *testing whether undoing the suspected cause flips the outcome*. It now does exactly that:

1. **Select hunks** (`select_cause_hunks`, deterministic, no model). A hunk of the failing commit's diff is selected if either:
   - its file path or basename appears in the cause or evidence, or
   - a distinctive identifier from its changed lines appears there as a whole word. Language keywords and names like `self`, `None` and `len` are ignored.

   If nothing matches and the diff has exactly one hunk, that hunk is selected. Otherwise the result is **inconclusive**. Reverting the whole commit would only show that *the commit* is responsible, not the specific cause that was claimed.
2. **Reverse-apply** only those hunks (`git apply -R`) in a worktree at the failing commit.
3. **Run the failing tests.** Node ids are parsed from `FAILED path::test` lines in the logs. If the logs have none, the whole suite runs.

| Outcome | Meaning |
|---|---|
| `pass` | Undoing the cause makes the failing tests pass. The cause is confirmed. |
| `fail` | The tests still fail with the cause undone. The cause is not sufficient. |
| `inconclusive` | The cause couldn't be isolated to hunks, the hunks didn't reverse-apply, the tests timed out, or no checkout is configured |

The Confidence Gate still requires `pass`, so **nothing auto-applies unless the claimed cause was actually confirmed by running tests.** No model is called, so this step no longer affects `attribution_source`, which is still decided only by the model calls for causes and evidence. Results are cached per (repo, commit, selected hunks).

Cost: one extra test run per semantic Run (targeted to the failing tests when the logs name them).

## Action Layer

**`apply_fix(run)`**

1. `should_apply_fix` must pass: kill switch off, verification passed, Confidence Gate and Scope Guard approved.
2. No `GITHUB_TOKEN` → `(False, "GITHUB_TOKEN not set; PR not opened")`. The action is recorded, and no URL is invented.
3. Re-apply the verified patch in a worktree and collect the changed files with their git modes and contents (`collect_patched_files`).
4. Make the GitHub REST calls:
   - `GET /git/commits/{failing sha}` to get the base tree
   - `POST /git/blobs` per file (base64)
   - `POST /git/trees` with `base_tree`
   - `POST /git/commits` with parent = the failing commit
   - `POST /git/refs` to create `refs/heads/kintsugi/fix-<run id>`
   - `POST /pulls` into the Run's branch

   The PR body carries the diagnosis, the executed counterfactual result, the evidence and the verification result.
5. Only `GITHUB_TOKEN` is used, never your local git credentials. The token appears only in the `Authorization` header, never in URLs, logs or error messages.

**`escalate(run, reason)`** opens `POST /issues` with the escalation trace, including the tail of the last test output. Without a token it records `status: not_posted` and returns `(False, reason)`. The pipeline writes an audit entry either way, so no escalation is silent.

Nothing is ever merged. A PR is a proposal for a human to review.

## Tests (`tests/test_sandbox.py`)

The tests build a real throwaway git repo. Commit 1 has `total()` return 42 and passing tests. Commit 2 changes it to `return 41`, which is the failing commit. The repo's tests run with the current interpreter's pytest. GitHub is replaced by a fake transport that records requests; no network is used.

The suite covers:

- a correct patch passes
- a model-style fenced patch still applies
- a wrong patch fails on the tests
- a non-applying patch fails
- pseudocode is rejected
- a missing checkout fails
- a mechanical retry of a genuinely broken commit fails
- a missing test runner fails
- the env scrubbing works
- reverting the real cause gives `pass`
- reverting an unrelated hunk gives `fail`
- hunk selection by identifier and by file, and refusing to guess between multiple hunks
- the full PR API call sequence (base tree, parent, base branch, auth header)
- no token → no PR and no fake URL
- issue creation, and escalation without a token recorded as `not_posted`
