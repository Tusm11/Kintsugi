# Fix-Cache

`src/fix_cache.py`, `src/cache_metrics.py` · tests: `tests/test_fix_cache.py` (every test runs on both the in-memory and the Redis backend, using fakeredis)

## What goes in

Only fixes that **the Verifier passed**, taken from **model-sourced** attributions. `FixCache.store()` refuses `fallback_heuristic` attributions and empty patches, and records a `store.skipped` metric when it does. The pipeline calls `store()` only after a Verifier pass.

Each entry is keyed by `(repo, attribution fingerprint)`:

```
fingerprint, repo,
fix_patch,              # PROPOSED_FIX section of the model response
attribution,            # full Attribution dict
verification_result,    # the Verifier's test_results
failure_signature,      # see below; may be None
features,               # pre-computed similarity features (avoids re-normalizing on every scan)
created_at, last_seen, hit_count
```

Entries are **scoped per repo**. A patch only means something against the repo it was verified in, and reusing it in another repo would leak code between repos.

## What a hit buys you

| Lookup result | Pipeline behaviour | Saved |
|---|---|---|
| **Signature EXACT** (pre-attribution) | Cached attribution recorded as this Run's attribution; cached patch reused | Attribution Engine (~4 model calls) + generation |
| **Fingerprint EXACT** (post-attribution) | Cached patch reused | Generation |
| **NEAR** | Cached *attribution* becomes the bucket-1 seed. **The patch is never applied.** | Nothing directly. The first attempt starts warm instead of cold. |
| **NONE** | Cold start | — |

In every EXACT case the cached patch goes through Output Guardrail → Confidence Gate → Scope Guard → Verifier, exactly like a generated patch. A reused patch never skips verification.

## Two lookup paths

The fingerprint comes from an Attribution, and producing an Attribution is the Attribution Engine's job. A fingerprint lookup alone therefore cannot let the pipeline "skip the Attribution Engine". Two lookup paths solve this:

### 1. `lookup_by_signature(run)`: before attribution

`failure_signature(run)` is deterministic and uses no model. It hashes:

- `repo`
- exception class names found in the logs (`AssertionError`, `KeyError`, …)
- failing test ids from `FAILED …` / `ERROR …` lines, with parametrize ids stripped
- up to 3 assertion/error lines, normalized with the same `normalize_cause` so numbers and identifiers collapse
- the set of files changed in the diff

If the logs carry none of the first three items, the function returns **None** and the Run never takes this path. Two empty signatures would otherwise match each other.

The signature is stored as a pointer, `sig → fingerprint`, with the same TTL as the entry.

**Trade-off:** on a signature hit, the Confidence Gate judges the *cached* attribution rather than a fresh one. The Verifier still decides, but the diagnosis is borrowed. `FIX_CACHE_SIGNATURE_REUSE=false` turns this path off and leaves only path 2.

### 2. `lookup_by_attribution(repo, attribution)`: after attribution

1. Direct `O(1)` read of `entry:{fingerprint}`, which works for both hot and cold entries.
2. Scan up to `near_scan_limit` fingerprints from the hot tier, most recently seen first.
3. `AttributionSimilarity.best_match` picks the strongest tier, then the highest score.

Any hit (EXACT or NEAR) *touches* the entry: `hit_count += 1`, `last_seen = now`, and promotion into the hot tier. A lookup hit does **not** refresh the TTL.

## Storage layout (Redis)

```
kintsugi:fixcache:{repo}:entry:{fingerprint}   Hash        cold tier, EXPIRE = TTL
kintsugi:fixcache:{repo}:sig:{signature}       String      → fingerprint, EX = TTL
kintsugi:fixcache:{repo}:hot                   Sorted Set  member = fingerprint, score = last_seen
```

This follows the v1 README's "Sorted Set (hot) + Hash (cold) with TTL, spill-not-discard" design:

- **Hot tier** = the Sorted Set, capped at `hot_capacity` (default 200). Only hot entries are scanned for NEAR matches, which bounds lookup cost.
- **Cold tier** = the per-entry Hashes. Trimming the hot set (`ZREMRANGEBYRANK`) removes only the index membership, never the Hash. A spilled entry is still reachable by exact fingerprint or signature, and any hit promotes it back to hot.
- Hot members whose Hash has expired are removed lazily the next time a scan finds them.

`InMemoryFixCacheBackend` reproduces the same semantics in process, including TTL, with an injectable clock for tests. It is the default when `REDIS_URL` is unset. It is **not shared across worker processes**, so production multi-worker setups need Redis.

## Invalidation

| Trigger | Effect |
|---|---|
| **TTL** (default 7 days) | The entry and signature pointer expire. The repo drifts, so old fixes age out. |
| **Verified reuse** | `record_reuse_result(passed=True)` refreshes the TTL on the entry and the signature. A patch that just passed against the current code has earned more time. |
| **Failed re-verification** | `record_reuse_result(passed=False)` → `bust()`. The entry, signature pointer and hot membership are deleted **before** the pipeline falls back to fresh generation, so a bad reuse cannot stay in the cache. |
| **Output Guardrail rejects a cached patch** | `bust(reason="output_guardrail")`. This means the guard rules have tightened since the fix was cached. |
| Confidence Gate / Scope Guard rejects on reuse | **No bust.** Those gates judge this Run's attribution and diff, not the patch. |
| Newer verified fix for the same fingerprint | `store()` overwrites the patch but keeps `created_at` and `hit_count`, and moves the signature pointer. |

A lookup hit alone never extends an entry's life. Only evidence from the Verifier does.

## Metrics (`src/cache_metrics.py`)

`CacheMetrics.record(event, **fields)` increments a counter, keeps the last 1000 events in memory, and appends one JSON line to `FIX_CACHE_METRICS_PATH` when that is set. A metrics failure never breaks a Run.

| Event | Meaning |
|---|---|
| `lookup.signature_hit` / `lookup.signature_miss` | Pre-attribution path |
| `lookup.exact` / `lookup.near` / `lookup.none` | Post-attribution path (includes `score` and candidate count) |
| `reuse.verified_pass` / `reuse.verified_fail` | Verifier's verdict on a reused patch |
| `bust` | Includes `reason` |
| `store` / `store.skipped` | Write path |
| `bucket.used` | One per semantic attempt: `bucket_tier`, `seeded`, `tokens_used`, `approx_context_tokens`, `truncated`, `generation_ok` |

`bucket.used` also produces the per-tier counters `bucket.used.bucket_{1,2,3}` and `bucket.used.tokens`.

`CacheMetrics.snapshot()` (also available as `KintsugiPipeline.get_cache_metrics()` and under `get_statistics()['fix_cache']`) adds these rates: `exact_hit_rate`, `near_hit_rate`, `signature_hit_rate`, `reuse_pass_rate`, `busts`.

**Signals to watch in the tablib pass:**

- A rising bust rate or a falling `reuse_pass_rate` means the tiers are too loose or the TTL is too long.
- A near-zero `exact_hit_rate` together with a healthy `near_hit_rate` means normalization is too strict.
- `bucket.used.tokens` per tier shows whether bucket 1 is actually cheaper in practice.

Every cache decision is also written to the Run's audit trail as `event_type: "fix_cache"` entries (`lookup`, `reuse`, `bust`, `store`, `bucket`). Patch contents are never included.
