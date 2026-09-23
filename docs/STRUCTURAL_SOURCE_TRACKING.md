# Structural Attribution Source Tracking Fix

## Problem Statement

The original `attribution_source` detection was **fragile** — it used string-matching on model output text to infer whether a fallback was used:

```python
# OLD (FRAGILE):
if "Unable to determine cause" in cause or "model analysis failed" in details:
    attribution_source = "fallback_heuristic"  # inferred from text
else:
    attribution_source = "model"  # inferred from text
```

This introduced two critical silent-degradation risks:

### Risk 1: False Negative (Low-Confidence Model Output Mislabeled as Fallback)
If a real, successful model call produces output containing similar phrases:
```
"I was unable to determine a clear cause here due to complex logic"
```
This gets mislabeled as fallback, even though it came from a successful model call (`success=True`).

Result: **Low-confidence but valid model reasoning is treated as untrustworthy fallback.**

### Risk 2: False Positive (Fallback Mislabeled as Model Success)
If a fallback path's exception message uses different wording than expected:
- API timeout with custom message: "Request timed out after 30s"
- Malformed response that doesn't hit the expected error path
- Rate limit with different error format

These don't match the string patterns, so the fallback is mislabeled as `"model"`.

Result: **Untrustworthyfall-back heuristics are marked as legitimate model reasoning.**

This is exactly the silent-degradation bug the Confidence Gate was supposed to prevent.

---

## Solution: Structural Source Tracking

Set `attribution_source` **at the call site**, based on whether `provider.call_with_retry()` actually succeeded:

```python
# NEW (STRUCTURAL):
success, response = provider.call_with_retry(prompt, budget_tokens=500)

if success:
    attribution_source = "model"       # Structural fact: API succeeded
else:
    attribution_source = "fallback_heuristic"  # Structural fact: API failed
    # Explicitly invoke fallback heuristics here
```

**Key principle**: Source tracking is now a **structural fact** about what happened (did the API call succeed?), not an **inference** about what the output looks like (does it contain these keywords?).

---

## Implementation

### Four Methods Refactored

All attribution-gathering methods now return `Tuple[Result, str]` with source:

#### 1. `_extract_suspected_causes(diff, failure_logs) → Tuple[List[str], str]`
```python
success, response = provider.call_with_retry(prompt, budget_tokens=500)
if success:
    source = "model"
    causes = [parse model response]
else:
    source = "fallback_heuristic"
    causes = [parse diff for patterns]
return (causes, source)
```

#### 2. `_gather_evidence_for_cause(...) → Tuple[List[str], str]`
```python
success, response = provider.call_with_retry(prompt, budget_tokens=300)
if success:
    source = "model"
    evidence = [parse model response]
else:
    source = "fallback_heuristic"
    evidence = [basic pattern matching]
return (evidence, source)
```

#### 3. `_gather_evidence_against_cause(...) → Tuple[List[str], str]`
Same pattern: source set based on API success flag, not output inspection.

#### 4. `_test_counterfactual(cause, diff, run) → Tuple[CounterfactualResult, str]`
Same pattern: source set based on API success flag, not output inspection.

### Composite Rule in `attribute()`

The final `Attribution.attribution_source` is set to `"fallback_heuristic"` if **ANY** method used fallback:

```python
sources = []
causes, src = self._extract_suspected_causes(diff, failure_logs)
sources.append(src)

evidence_for, src = self._gather_evidence_for_cause(cause, diff, failure_logs, files)
sources.append(src)

# ... more methods ...

# Trust only if ALL succeeded
final_source = "fallback_heuristic" if any(s == "fallback_heuristic" for s in sources) else "model"
```

This ensures: **If any part of the attribution pipeline falls back, the entire attribution is marked as fallback-sourced and ineligible for auto-apply.**

---

## Confidence Gate Integration

The `ConfidenceGate` already checks `attribution_source` first:

```python
def is_eligible_for_auto_apply(self, attribution: Attribution) -> Tuple[bool, str]:
    # STRUCTURAL check: always reject fallback-sourced
    if attribution.attribution_source == "fallback_heuristic":
        return (False, "Attribution based on fallback heuristics, not real model reasoning — ineligible for auto-apply")
    
    # Then check evidence structure
    if not attribution.evidence_for:
        return (False, "No supporting evidence")
    
    # ... more structural checks ...
```

Now this works correctly: a fallback-sourced attribution is **always rejected**, regardless of how clean the evidence looks.

---

## Test Coverage

11 unit tests verify:

1. ✓ Model success → `source = "model"`
2. ✓ Model failure → `source = "fallback_heuristic"`
3. ✓ All four methods (extract, for, against, counterfactual) return tuples with source
4. ✓ **Composite rule**: If ANY method uses fallback, final source is "fallback_heuristic"
5. ✓ **No string inspection**: Output containing "Unable to determine" with `success=True` still yields `source="model"`

All tests pass: `tests/test_attribution_structural_source.py` (11/11 passing)

---

## Guarantees

✅ **No more false negatives**: Real model reasoning is never mislabeled as fallback just because of cautious wording  
✅ **No more false positives**: Fallback heuristics are always caught and marked  
✅ **Structural over inferred**: Source is set from API facts, not text-matching  
✅ **Confident Gate integration**: Fallback rejections work correctly  
✅ **Audit trail**: Every source assignment is explicit and tied to provider behavior  

---

## Migration Notes

If you have existing code referencing the old `Attribution` model:
- `attribution_source` field now set **correctly** (structurally, not by text-matching)
- `evidence_strength_source = "n/a"` (strength scores removed)
- No breaking API changes for consumers of `Attribution`

All downstream usage (Confidence Gate, repair handlers, logging) works unchanged.
