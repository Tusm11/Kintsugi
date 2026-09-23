# Kintsugi v2: Fix-Cache and Context-Bucket Tiering

v2 adds two cost reducers to the **semantic** repair path. It does not change a single trust rule:

- **Fix-cache.** When a root cause has already been fixed and verified, the old fix is reused. Kintsugi can then skip diagnosis, generation or both.
- **Context-bucket tiering.** Each semantic retry gets a bounded slice of context. The slice widens only when a narrower one has already failed. This replaces v1's approach of sending everything on every attempt.

Both were listed in the v1 README under *What's Deferred*. Both depend on one new function, which scores how similar two `Attribution` records are.

> **Unchanged invariant:** every patch still passes Output Guardrail → Confidence Gate → Scope Guard → Verifier before the Action Layer sees it, whether the patch came from the cache or from a model. Caching saves diagnosis and generation cost. It never saves verification cost.

## Component docs

| Doc | Component | Source | Tests |
|---|---|---|---|
| [ATTRIBUTION_SIMILARITY.md](ATTRIBUTION_SIMILARITY.md) | Fingerprinting, similarity score, EXACT/NEAR/NONE tiers | `src/attribution_similarity.py` | `tests/test_attribution_similarity.py` |
| [FIX_CACHE.md](FIX_CACHE.md) | Read/write path, Redis hot/cold layout, invalidation, metrics | `src/fix_cache.py`, `src/cache_metrics.py` | `tests/test_fix_cache.py` |
| [CONTEXT_BUCKETS.md](CONTEXT_BUCKETS.md) | Bucket 1/2/3 contents, budgets, truncation | `src/context_buckets.py` | `tests/test_context_buckets.py` |
| [SEMANTIC_PATH.md](SEMANTIC_PATH.md) | How the pipeline wires the pieces together | `src/pipeline.py` (`_process_semantic_run` and helpers) | `tests/test_pipeline_fix_cache.py` |
| [REAL_EXECUTION.md](REAL_EXECUTION.md) | Real Verifier (git worktree + test run), executed counterfactual, GitHub PR/issue Action Layer; what used to be simulated | `src/sandbox.py`, `src/verifier.py`, `src/attribution.py`, `src/action_layer.py`, `src/github_client.py` | `tests/test_sandbox.py` |

## Build order (as designed) and where each step landed

1. **Attribution fingerprinting/similarity:** `attribution_similarity.py`. This module is pure, with no I/O, and includes `calibration_report()` for checking it against labelled past records.
2. **Fix-cache read/write, exact match only, verification mandatory:** `fix_cache.py` and `SemanticHandler.add_cached_repair_to_run` + `KintsugiPipeline._attempt_cached_fix`.
3. **Near-match path into bucket 1:** `FixCache.lookup_by_attribution` returns NEAR, and `ContextBucketBuilder.build(seed_attribution=...)` uses it.
4. **Bucket 2/3 tiering:** `KintsugiPipeline._semantic_generation_loop` and `bucket_tier_for_attempt`.
5. **Invalidation + hit/miss metrics:** TTL, `FixCache.bust`, `record_reuse_result`, `cache_metrics.py` (JSONL sink for the tablib pass).

## One deliberate addition to the design: the failure signature

The design says an EXACT match should skip the Attribution Engine entirely. The cache is keyed by an **attribution** fingerprint, though, and producing an attribution *is* the Attribution Engine's work (about 4 model calls). A pure fingerprint lookup can therefore skip generation, but not diagnosis.

To skip diagnosis as well, the cache keeps a secondary index: a deterministic **failure signature**. It combines repo, exception types, failing test ids, normalized assertion text and the changed-file set, and it uses no model. Each signature points at a fingerprint:

- **Signature hit** (before attribution): reuse the cached attribution and patch. Attribution and generation are both skipped.
- **Fingerprint hit** (after attribution): reuse the patch. Only generation is skipped.

Signature reuse is the more aggressive of the two, because the Confidence Gate then judges the *cached* attribution. It can be switched off with `FIX_CACHE_SIGNATURE_REUSE=false`, which leaves only the post-attribution path from the original design. See FIX_CACHE.md → *Two lookup paths*.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `FIX_CACHE_ENABLED` | `true` | `false` disables the cache. Buckets still apply. |
| `REDIS_URL` | unset | Set to use Redis. Unset means an in-process in-memory backend. |
| `FIX_CACHE_TTL_SECONDS` | `604800` (7 days) | Entry lifetime. Refreshed only by a *verified* reuse. |
| `FIX_CACHE_HOT_CAPACITY` | `200` | Size of the per-repo hot Sorted Set that is scanned for near matches |
| `FIX_CACHE_NEAR_SCAN_LIMIT` | `50` | Maximum hot entries compared per near-match lookup |
| `FIX_CACHE_NEAR_THRESHOLD` | `0.65` | Minimum overall similarity for NEAR |
| `FIX_CACHE_SIGNATURE_REUSE` | `true` | Allows the pre-attribution signature shortcut |
| `FIX_CACHE_METRICS_PATH` | unset | Appends every cache/bucket event as JSON lines |
| `MAX_RETRIES_SEMANTIC` | `2` | Unchanged. Also caps how many buckets are used (2 means buckets 1 and 2). |
| `KINTSUGI_REPO_PATH_<REPO>` / `KINTSUGI_REPO_PATHS` | unset | Local checkout per repo. **Required** for verification and counterfactuals. Without it both fail or come back inconclusive, and nothing auto-applies. |
| `KINTSUGI_TEST_COMMAND` | `python -m pytest -q --tb=short` | Test command run in the sandbox worktree |
| `KINTSUGI_TEST_TIMEOUT_SECONDS` | `600` | Per test run |
| `GITHUB_TOKEN` | unset | Needed to open PRs and issues. Without it they are recorded locally and not posted. |

See [REAL_EXECUTION.md](REAL_EXECUTION.md) for the rest.

`KintsugiPipeline(fix_cache=..., use_fix_cache=False, context_builder=...)` overrides these in code.

## Pre-existing v1 bugs fixed along the way

The v2 semantic path could not work without these fixes. They also explain some v1 integration tests that were already failing.

1. **Semantic runs could never be healed.** `ActionLayer.should_apply_fix` requires Confidence Gate and Scope Guard decision steps on the Run. v1's `process_run` built those steps and discarded them, so `apply_fix` always refused. v2's `_check_gates` now appends them. The mechanical/structural v1 path still discards them, so it can never open a PR (mechanical Runs also have no attribution, so the Confidence Gate always escalates them).
2. **`SemanticHandler.create_repair_step` crashed.** It read `run.steps[-1].attribution.to_dict()`. In the real pipeline order `steps[-1]` is the routing step, whose attribution is `None`. It now uses the most recent attribution step.
3. **The Output Guardrail scanned a hard-coded string** (`"def fix(): return True"`) instead of the patch. It now scans the real patch on both the semantic path and the v1 mechanical/structural path.
4. **The Verifier, counterfactual and Action Layer were simulated** (always-pass tests, a keyword-guessed counterfactual, fake PR/issue URLs). All three now run for real. See [REAL_EXECUTION.md](REAL_EXECUTION.md).

## Known limits (not addressed by v2)

- **The AttributionEngine returns filler as evidence.** When the model finds nothing, strings like `"No contradicting evidence found"` land in `evidence_against`, and the Confidence Gate then rejects the Run because `evidence_against` is non-empty. The similarity function ignores these strings, but the gate does not.
- **Scope Guard checks `run.diff`, not the patch.** It measures the commit that broke CI rather than the proposed or cached patch. The same limitation existed in v1.
- **No checkout, no neighbouring code.** Bucket 2's "surrounding code" needs a `code_context_provider`, because the webhook carries only the diff and logs.
