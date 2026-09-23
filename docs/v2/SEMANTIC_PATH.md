# The v2 Semantic Path in the Pipeline

`src/pipeline.py`: `_process_semantic_run` and its helpers · tests: `tests/test_pipeline_fix_cache.py`

Rate guard, input guardrail and classification run exactly as in v1. After classification, **semantic** Runs branch into `_process_semantic_run`. Mechanical and structural Runs continue through the unchanged v1 flow.

## Flow

```
classified: semantic
      │
      ▼
A. FixCache.lookup_by_signature(run)             (no model; skipped if cache disabled)
      │ EXACT                          │ miss
      ▼                                │
   record cached attribution           │
   route                               │
   _attempt_cached_fix ──► healed / escalated (gate)
      │ cached patch failed → busted   │
      └──────────────┬─────────────────┘
                     ▼
B. AttributionEngine.attribute(run)              (fresh diagnosis)
   route (if not already)
   FixCache.lookup_by_attribution(repo, attribution)
      │ EXACT              │ NEAR                 │ NONE
      ▼                    │                      │
   _attempt_cached_fix     │ seed = cached        │ seed = None
      │ busted → NONE      │ attribution          │
      └────────────┬───────┴──────────────────────┘
                   ▼
C. _semantic_generation_loop
   attempt 1 → bucket 1 (seeded if NEAR) → generate → guard → gates → verify
   attempt 2 → bucket 2                  → ...
   attempt 3 → bucket 3 (only if MAX_RETRIES_SEMANTIC ≥ 3)
      │ verified                                  │ budget exhausted
      ▼                                           ▼
   FixCache.store(...)  →  Action Layer: open PR   escalate (with last failure reason)
```

## `_attempt_cached_fix(run, lookup)`

This runs a cached patch through the same chain a generated patch goes through:

1. `SemanticHandler.add_cached_repair_to_run` appends a REPAIR step with `source: "fix_cache"`, the cached patch, `tokens_used: 0` and the lookup path. **No semantic retry is charged**, because no model is called.
2. **Output Guardrail** scans the cached patch.
   - If rejected: `bust(reason="output_guardrail")`, then return `None` so the caller falls through to fresh work.
3. **Confidence Gate + Scope Guard** (`_check_gates`), with their decision steps appended to the Run.
   - If either fails: **escalate** (terminal) and do not bust, because the rejection says nothing about the patch.
4. **Verifier.**
   - Pass: `record_reuse_result(passed=True)` refreshes the TTL, then the Action Layer opens a PR → `HEALED`.
   - Fail: `record_reuse_result(passed=False)` busts the entry, then return `None` → fall through.

After a fall-through from path A, the Run gets a **fresh** attribution step. The Confidence Gate always reads the most recent attribution step, so later gates judge the fresh diagnosis rather than the busted cached one.

## `_semantic_generation_loop(run, attribution, lookup)`

For each attempt:

1. Build bucket `min(attempt + 1, 3)`, seeded with the NEAR entry's attribution if there is one.
2. `SemanticHandler.add_repair_to_run(context_bucket=..., attempt_number=...)` records a semantic retry with the Budget Guard. If the budget is already spent, it refuses and appends no step, and the loop ends.
3. Record metrics (`bucket.used`) and the audit entries for the bucket summary and repair attempt.
4. If generation succeeded:
   - **Output Guardrail** on the extracted `fix_patch`. A rejection escalates immediately with the v1 message shape (`"Output guardrail: …"`).
   - **Gates.** A failure escalates immediately (`"Failed Confidence gate"` / `"Failed Scope guard"`).
   - **Verifier.** A pass stores the fix in the cache (attribution, patch, `test_results`) and opens a PR.
5. Otherwise, note the failure reason (generation failed / verification failed) and continue if `BudgetGuard.can_retry(run, "semantic")`.

If the loop ends without any attempt reaching the gates (for example, every model call failed), the gates are still evaluated once so the audit trail matches v1, and the escalation names the first gate that fails, or `"Repair failed"`.

## Audit trail additions

Every Run on this path writes `event_type: "fix_cache"` entries:

| `cache_event` | Written when | Details |
|---|---|---|
| `lookup` | each lookup | path, tier, score, breakdown, fingerprint, reason |
| `reuse` | after verifying a cached patch | fingerprint, path, verified |
| `bust` | output-guardrail bust on reuse | fingerprint, reason |
| `store` | after a verified generated fix | fingerprint, stored |
| `bucket` | each generation attempt | the bucket summary: tier, sections, sizes, truncated, seeded (no content) |

The repair step's `input.context_bucket` carries the same bucket summary. `input.source` is `"generation"` or `"fix_cache"`.

## Wiring options

```python
KintsugiPipeline()                                   # cache from env (in-memory unless REDIS_URL)
KintsugiPipeline(use_fix_cache=False)                # buckets only, no cache
KintsugiPipeline(fix_cache=FixCache(backend=RedisFixCacheBackend.from_url(url)))
KintsugiPipeline(context_builder=ContextBucketBuilder(code_context_provider=my_provider))
pipeline.get_cache_metrics()                         # counters + hit/pass rates
```

## What the tests pin down

| Behaviour | Test |
|---|---|
| Cold run uses bucket 1, heals, stores | `TestColdPath::test_cold_run_heals_uses_bucket1_and_stores` |
| Signature hit skips attribution **and** generation | `TestExactReuse::test_signature_hit_skips_attribution_and_generation` |
| Reuse still calls gate, scope and Verifier exactly once each | `TestExactReuse::test_exact_reuse_still_runs_gates_and_verifier` |
| Fingerprint EXACT when the signature differs | `TestExactReuse::test_fingerprint_exact_when_signature_differs` |
| Failed re-verification busts, regenerates, re-caches | `TestExactReuse::test_failed_reverification_busts_and_falls_back_to_generation` |
| Scope failure on reuse escalates without busting | `TestExactReuse::test_scope_failure_on_reuse_escalates_without_busting` |
| NEAR seeds bucket 1 and never applies the cached patch | `TestNearAndBuckets::test_near_match_seeds_bucket1_and_never_applies_cached_patch` |
| Verification failure widens to bucket 2, then escalates; nothing unverified cached | `TestNearAndBuckets::test_verification_failure_widens_to_bucket2_then_escalates` |
| Bucket 3 only with semantic budget ≥ 3 | `TestNearAndBuckets::test_bucket3_reached_when_semantic_budget_is_3` |
| Output Guardrail rejection is not retried | `TestNearAndBuckets::test_output_guardrail_rejection_is_not_retried` |
