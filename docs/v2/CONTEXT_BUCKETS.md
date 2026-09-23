# Context-Bucket Tiering

`src/context_buckets.py` · prompt: `SemanticRepairPrompt.generate_from_bucket` in `src/prompts.py` · tests: `tests/test_context_buckets.py`

## Problem

In v1 every semantic attempt got the same prompt: full logs, full diff and commit message. A retry cost as much as the first attempt and added no new information. The v1 README flagged this context bloat as the reason tiering was deferred.

## Idea

Each semantic attempt gets a **bucket**: a bounded, prioritized slice of context. The slice widens only after a narrower one has failed.

| Attempt | Bucket | Contents (in priority order) | Default budget |
|---|---|---|---|
| 1 | **1** | NEAR-match seed attribution (if the fix-cache found one), then the current diagnosis (cause, counterfactual, evidence for/against), then a focused log excerpt | 6,000 chars (~1.5k tokens) |
| 2 | **2** | bucket 1 + the recent diff + surrounding code (if available) | 14,000 chars (~3.5k tokens) |
| 3+ | **3** | seed + current diagnosis *with alternatives considered* + **full** log + diff + surrounding code + commit message | unbounded |

The mapping is `bucket_tier_for_attempt(n) = min(n + 1, 3)`.

### Interaction with the retry budget

Buckets do **not** add attempts. Each attempt still consumes one semantic retry through `BudgetGuard.record_retry(run, "semantic")`, and the loop continues only while `BudgetGuard.can_retry(run, "semantic")` is true. That covers both the per-layer limit and the total ceiling.

With the default `MAX_RETRIES_SEMANTIC=2`, only buckets 1 and 2 are ever used. Bucket 3, the expensive fallback, is reached only when an operator raises the semantic budget to 3 or more.

## The pieces of a bucket

**Seed (bucket 1 and up).** When `FixCache.lookup_by_attribution` returns NEAR, the cached *Attribution* goes into the bucket, labelled with its similarity score. The cached patch is never included. The prompt tells the model that the seed is a hint and that the current diagnosis and logs take precedence. A NEAR match means "similar", not "same", and handing over the old patch would push the model toward copying it.

**Focused log excerpt (buckets 1–2).** `focused_log_excerpt` keeps ±3 lines around every line that looks like an error (`error|exception|assert|failed|traceback|expected`), inserts `...` between gaps, and caps the result at 40 lines. When nothing matches, it returns the log's tail.

**Surrounding code (bucket 2 and up).** The webhook delivers only the diff and logs, not a checkout. To give the model adjacent functions, pass a provider:

```python
ContextBucketBuilder(code_context_provider=lambda run: read_neighbours(checkout, run.diff))
```

If there is no provider, the builder uses `run.metadata["surrounding_code"]` when present. If neither exists, the section is left out and bucket 2 adds only the diff. Any exception raised by the provider is swallowed, because a context helper must never break a repair.

## Budgets and truncation (`ContextBucketBuilder._fill`)

Sections are added in the priority order above:

1. If the whole section fits, it is added.
2. The first section that crosses the budget is **truncated**, with a `[... truncated to fit context bucket ...]` marker, and every later section is **dropped**.
3. The first section is always kept, cut hard if necessary, so an attempt never runs without its core diagnosis.

The rendered bucket stays within its budget whenever the budget is at least as long as the first section's heading line. `bucket.truncated` and `bucket.dropped_sections` record what the model did not see. Both appear in the repair step's `input.context_bucket` and in the audit log, so an escalation shows whether the model was working with partial context.

Budgets are characters, used as a cheap proxy for tokens (~4 chars/token). Override them per tier:

```python
ContextBucketBuilder(char_budgets={1: 4000, 2: 10000, 3: 40000})
```

## Prompt

`SemanticRepairPrompt.generate_from_bucket(bucket_text, bucket_tier, attempt_number, seeded)` wraps the rendered bucket with the instructions and keeps **the same response format** as v1 (`ROOT_CAUSE / PROPOSED_FIX / WHY_THIS_WORKS / RISK_ASSESSMENT`), so response parsing is unchanged. The wrapper states which bucket the model is seeing ("bucket N of 3"), notes on retries that the previous attempt did not produce a verified fix, and adds the seed disclaimer when a seed is present.

`extract_fix_patch()` in `src/handlers.py` takes the `PROPOSED_FIX` section as the patch, which is what gets scanned, verified and cached. If the model ignored the format, it falls back to the whole response rather than an empty patch.

## What triggers the next bucket

| Outcome of attempt N | Next step |
|---|---|
| Model call failed | Retry with bucket N+1, if budget allows |
| Verifier failed the patch | Retry with bucket N+1, if budget allows |
| Output Guardrail rejected the patch | **Escalate.** More context does not make an unsafe patch acceptable. |
| Confidence Gate / Scope Guard failed | **Escalate.** These gates judge the attribution and diff, which more context does not change. |
| Verifier passed | Cache the fix, open the PR |
