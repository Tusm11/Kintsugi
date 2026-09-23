# Attribution Similarity

`src/attribution_similarity.py` · tests: `tests/test_attribution_similarity.py`

## Why this is the core

The fix-cache and the context buckets both depend on one question: *is this failure's root cause the same as one we already fixed?* The answer is also the highest-risk part of v2. A similarity function that is too loose hands wrong patches to the Verifier and fills bucket 1 with irrelevant seeds. One that is too strict never hits. Either way the system ends up worse than having no cache.

Exact text matching cannot answer the question. Two occurrences of the same root cause differ in line numbers, variable names, file paths and the model's wording. So the comparison uses only the Attribution's **stable parts**.

The module is pure: no I/O, no model calls, no Redis. It can be tested and calibrated in isolation.

## Step 1: Reduce an Attribution to features

`extract_features(attribution) -> AttributionFeatures`

### claimed_cause → normalized structural pattern (`normalize_cause`)

The steps run in this order:

1. Strip model labels and list markers (`ROOT_CAUSE:`, `1.`, `-`).
2. **Keep** exception class names, lower-cased (`KeyError` → `keyerror`). A `KeyError` and a `TypeError` are different root causes, so they must not collapse together.
3. Replace instance-specific details with placeholders:
   - quoted literals → `<str>`
   - file paths (`src/cart.py`, `a/b/c`) → `<path>`
   - `line 42` / `col 3` → removed completely
   - identifiers (snake_case, camelCase, PascalCase, dotted names, anything called like `foo(`) → `<id>`
   - numbers → `<num>`
4. Lower-case the text, tokenize, drop stopwords (including filler such as "likely" and "suspected"), apply light suffix stemming (`removed`/`removes` → `remov`), and collapse runs of the same placeholder.

```
"Off-by-one in calculate_total() at line 42 of src/cart.py"  →  [off, one, <id>, <path>]
"Removed return statement in compute_price() in billing/price.py line 10"
"Removed return statement in get_total_amount() in orders/total.py line 212"
      → both: [remov, return, statement, <id>, <path>]
```

### evidence_for / evidence_against → shape (`evidence_shape`)

The exact text is discarded. Each item is assigned to **one** category by the first rule that matches, in this order:

`placeholder` → `counterfactual` → `boundary` → `type` → `assertion` → `log` → `control_flow` → `state` → `diff` → `other`

`placeholder` catches filler strings that the AttributionEngine itself emits when it found nothing ("No contradicting evidence found", "Limited evidence available - model unavailable"). These items are **not counted**, because they are not evidence.

The resulting shape is `(count, {category: n})`.

### counterfactual_result

Kept as-is (`pass` / `fail` / `inconclusive`). A missing value is treated as `inconclusive`.

## Step 2: Fingerprint

`AttributionFeatures.fingerprint` is the SHA-256 (truncated to 32 hex characters) of:

```json
{ "v": FINGERPRINT_VERSION,
  "cause": "<normalized tokens joined>",
  "for": [count_bucket, sorted category set],
  "against": [count_bucket, sorted category set],
  "cf": "pass" }
```

The count is bucketed (`0`, `1`, `2-3`, `4+`) because model output length is noisy. The number of evidence lines a model returns varies between otherwise identical calls.

**Bump `FINGERPRINT_VERSION` whenever normalization or categorization rules change.** Fingerprints from different rule versions must never compare equal, and a version bump makes every existing cache entry unreachable, which is the correct result.

## Step 3: Similarity score

A weighted match across the four fields, never a raw text diff:

| Field | Weight | Measure |
|---|---|---|
| cause | 0.55 | Jaccard over normalized token sets. A cause made only of placeholders scores 0 because it is uninformative. |
| evidence_for | 0.20 | 0.7 × weighted-Jaccard over category counts + 0.3 × count closeness |
| evidence_against | 0.10 | same as evidence_for |
| counterfactual | 0.15 | 1.0 if equal, 0.25 if either side is inconclusive, 0.0 if opposite (pass vs fail) |

The cause gets the largest weight because it is the only field that names the root cause. Evidence shape and counterfactual only corroborate it.

## Step 4: Tiers

`AttributionSimilarity.compare(current, cached) -> SimilarityResult(tier, score, breakdown, reason)`

| Tier | Rule | What the pipeline does with it |
|---|---|---|
| **EXACT** | Identical fingerprint **and** the current attribution is `attribution_source == "model"` | Reuse the cached patch. It still goes through all gates and the Verifier. |
| **NEAR** | score ≥ `near_threshold` (0.65) **and** cause similarity alone ≥ `min_cause_similarity` (0.5) | Use the cached *attribution* as the bucket-1 seed. Never apply the patch. |
| **NONE** | anything else | Cold start |

Design choices behind these rules:

- **EXACT means an identical fingerprint, not a high score.** A 0.97 score is still NEAR. The EXACT decision is deterministic and has no threshold that could drift.
- **A `fallback_heuristic` attribution is capped at NEAR.** Heuristic attributions carry little information and are the ones most likely to collide by accident. They could never pass the Confidence Gate anyway.
- **NEAR has a cause floor.** Most model outputs have 2–5 items in the "diff"/"assertion" categories, so evidence shapes look alike across unrelated failures. Without a separate cause floor, two unrelated failures could reach NEAR on evidence similarity alone.

`best_match(current, candidates)` ranks by tier first, then by score, and stops early on the first EXACT.

## Calibrating against real records

```python
from src.attribution_similarity import calibration_report
report = calibration_report([(attr_a, attr_b, same_root_cause_bool), ...])
# {'same_cause': {...}, 'different_cause': {...},
#  'false_exact': 0, 'false_near': n, 'missed_same': n}
```

- `false_exact` **must be 0.** Each false EXACT is a wrong patch that only the Verifier stands between and a PR.
- `false_near` wastes bucket-1 budget. A small number is tolerable.
- `missed_same` is lost savings.

Run this against labelled Attribution records from real Runs (for example from the tablib pass) before trusting the default thresholds. Then tune `near_threshold`, `min_cause_similarity` or the weights, which must sum to 1.0.
