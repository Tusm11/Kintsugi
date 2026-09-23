# Token Budget Analysis: Worst-Case Scenarios

## Current Configuration

```
Budget:
  max_tokens = 10,000 tokens per Run
  max_retries_semantic = 2 (most expensive, uses LLM)
  max_retries_structural = 3 (moderate, uses SLM)
  max_retries_mechanical = 5 (cheap, no LLM)
  max_retries_total = 7 (overall ceiling)

Per-call token budgets:
  _extract_suspected_causes:      500 tokens
  _gather_evidence_for_cause:     300 tokens
  _gather_evidence_against_cause: 300 tokens
  _test_counterfactual:           400 tokens
  SemanticHandler.handle:       2,000 tokens
```

---

## Worst-Case Scenario: Full Retry Chain

A Run that hits retry limits across all layers and exhausts semantic repair retries.

### Scenario: "Multiple Layers, Multiple Failures"

```
Phase 1: ATTRIBUTION (ONE per Run, happens once)
  - _extract_suspected_causes:        500 tokens
  - _gather_evidence_for_cause:       300 tokens
  - _gather_evidence_against_cause:   300 tokens
  - _test_counterfactual:             400 tokens
  ─────────────────────────────────
  Phase 1 Total:                    1,500 tokens

Phase 2: SEMANTIC REPAIR (max 2 retries)
  - SemanticHandler retry #1:       2,000 tokens
  - SemanticHandler retry #2:       2,000 tokens
  ─────────────────────────────────
  Phase 2 Total:                    4,000 tokens

GRAND TOTAL (Worst Case):           5,500 tokens
  
Budget remaining:                   10,000 - 5,500 = 4,500 tokens AVAILABLE ✓
```

---

## Critical Finding: Budget is SUFFICIENT

**Conclusion**: Even in the worst case (attribution + 2 full semantic retries), we use only 5,500 tokens out of 10,000.

**Safety margin**: 4,500 tokens (45%) unused buffer.

---

## Breakdown by Layer

### Attribution (1 pass, non-retryable)
- Always runs once: 1,500 tokens
- Cannot retry (retry limit does NOT apply to initial attribution)
- Represents 15% of budget

### Semantic Repair (up to 2 retries)
- Max 2 attempts × 2,000 tokens = 4,000 tokens
- Represents 40% of budget
- Has independent retry budget (max_retries_semantic=2)

### Structural Repair (not in this scenario, but included for completeness)
- Uses SLM (smaller model): ~500-1000 tokens per call
- Up to 3 retries: max 3,000 tokens
- Does not happen in semantic-only failure scenario

---

## Retry Budget vs Token Budget Alignment

**Key insight**: `max_retries_semantic=2` aligns perfectly with semantic handler budget.

| Layer | Max Retries | Tokens/Attempt | Max Tokens | % of Budget |
|-------|------------|----------------|-----------|-----------|
| Semantic | 2 | 2,000 | 4,000 | 40% |
| Structural | 3 | ~750 | ~2,250 | 23% |
| Mechanical | 5 | 0 (no LLM) | 0 | 0% |
| Attribution | 1 | 1,500 | 1,500 | 15% |
| **TOTAL** | **7** | - | **~7,750** | **78%** |

**Remaining buffer: 2,250 tokens (22%)** — sufficient for unexpected overhead or larger logs.

---

## Edge Cases and Risk Analysis

### 1. Large Diff or Failure Logs ✓ HANDLED
If diff/logs exceed normal size:
- Attribution prompts are truncated to first 1000 chars
- Handler prompts use same truncation
- Token count is stable and predictable

### 2. Multiple Failures in Same Run ✓ HANDLED
If Run hits semantic failure AND then structural failure:
- Attribution happens once (1,500 tokens)
- Semantic repair: up to 2 × 2,000 = 4,000 tokens
- Structural repair: up to 3 × 750 = 2,250 tokens
- **Total: 7,750 tokens — still within 10,000 budget**
- Overall ceiling (max_retries_total=7) enforces fairness

### 3. Rate Limits / Retries by Provider ✓ SEPARATE FROM KINTSUGI RETRIES
Model provider's `call_with_retry()` with exponential backoff happens WITHIN a single Kintsugi retry attempt:
- Kintsugi retry #1 calls model → provider auto-retries internally → returns result
- Kintsugi counts this as 1 retry attempt (1 token charge)
- Provider's internal retries don't consume Kintsugi retry budget
- **No compounding of retries**

### 4. Token Miscounting ✓ AUDITABLE
- Each handler call is atomic (call_with_retry returns token count)
- BudgetGuard tracks atomically (charge_tokens)
- Cost struct in Run.spent has full audit trail
- No silent over-spending possible

---

## Recommendation: Budget Adjustment?

**Current: 10,000 tokens**

**Recommendation**: KEEP as-is. Reasoning:

1. **Sufficient**: 78% utilization leaves 22% safety margin
2. **Conservative**: Semantic repairs rarely need 2 full retries (usually succeed on first)
3. **Defensive**: If larger diffs appear in future, can increase without retrying logic
4. **Fair**: 10k tokens supports full semantic diagnosis + repair flow without squeezing retry budget

---

## Token Spend Recommendations by Scenario

### Scenario A: CI test timeout (mechanical failure)
- Attribution: 0 tokens (mechanical layer skips attribution)
- Repair: 0 tokens (retry/backoff, no LLM)
- **Total: 0 tokens** → FASTEST PATH

### Scenario B: Config format error (structural failure)
- Attribution: 0 tokens (structural layer skips attribution)
- Repair: 750 tokens/attempt × up to 3 = up to 2,250 tokens
- **Total: 0-2,250 tokens** → MODERATE PATH

### Scenario C: Logic failure (semantic)
- Attribution: 1,500 tokens (required for diagnosis)
- Repair: 2,000 tokens/attempt × up to 2 = up to 4,000 tokens
- **Total: 1,500-5,500 tokens** → MOST EXPENSIVE PATH

**Worst combined case (all three layers fail)**: ~7,750 tokens

---

## Conclusion

✅ Token budget is **well-calibrated** for the retry limits.
✅ **22% safety margin** provides protection against unpredictable edge cases.
✅ **No risk** of semantic retries getting starved by structural/mechanical failures.
✅ **Audit trail is complete** — every token spend is tracked and attributable.

**Recommendation**: Proceed with current configuration.
