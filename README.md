# Kintsugi

**A self-healing agent for CI/CD pipeline failures — one that only fixes what it can prove, and never hides its uncertainty.**

> Named after the Japanese art of repairing broken pottery with gold — the repair is visible, and it makes the piece more valuable, not something patched over and hidden. That's the core philosophy of this system: repairs are explainable and traceable, never silent.

---

## Table of Contents

1. [What Problem This Solves](#what-problem-this-solves)
2. [What "Self-Healing" Means Here](#what-self-healing-means-here)
3. [Why CI/CD Specifically](#why-cicd-specifically)
4. [Core Data Model](#core-data-model)
5. [Full Pipeline Flow](#full-pipeline-flow)
6. [Component Reference](#component-reference)
7. [Guardrails](#guardrails)
8. [Model Provider Configuration](#model-provider-configuration)
9. [Redis — Where and Why](#redis--where-and-why)
10. [Skills (Extension Layer)](#skills-extension-layer)
11. [Design Principles](#design-principles)
12. [What's Deferred and Why](#whats-deferred-and-why)

---

## What Problem This Solves

Every agentic system that attempts to recover from its own failures faces the same unsolved question: **how do you know a fix is actually correct, not just plausible-looking?**

Most existing approaches fall into one of two camps:
- **Always auto-fix** — apply whatever the model proposes, trust it, move on. Fast, but dangerous — a confidently wrong fix is indistinguishable from a correct one until something breaks downstream.
- **Always escalate to a human/full LLM reasoning** — safe, but slow and expensive. Every failure, even a trivial network timeout, burns the same cost as a genuinely hard bug.

Kintsugi exists in the space these two extremes skip: a **tiered, budget-bounded, evidence-gated** repair system. Cheap, deterministic failures get fixed without ever touching an LLM. Genuinely uncertain failures get escalated honestly, with a full explanation of what was tried and why it wasn't resolved automatically. Nothing gets applied just because a model *said* it was confident.

---

## What "Self-Healing" Means Here

"Self-healing" is not one capability — it's three separate problems, and conflating them is where most systems go wrong.

### Layer 1 — Mechanical Repair
The failure is infrastructure-level: a timeout, a dead endpoint, a rate limit, a flaky network blip. Nothing about the code is wrong. This is handled deterministically — retry, backoff, or reroute — with **zero LLM involvement**.

### Layer 2 — Structural Repair
The output or config is malformed in a way a validator can catch: broken YAML, a missing field, a lint failure. The fix space is narrow enough that a small model (or occasionally a larger one, if the change is large in scope) can resolve it with a single, tightly-scoped correction.

### Layer 3 — Semantic Repair
Everything ran without crashing, but a test failed — the actual *logic* is wrong. Nothing "looks" broken from the outside; the failure is invisible by construction. This is the layer every other framework treats as a black box ("throw it back at a bigger LLM call"). Kintsugi treats it as a diagnosis problem: attribute the failure to a specific cause with real evidence, assess whether that evidence is strong enough to trust, and only then attempt a fix.

**The genuine contribution of this system is Layer 3** — using causal, evidence-backed attribution to decide not just *what* to fix, but *whether the system is actually sure enough to fix it automatically at all.*

---

## Why CI/CD Specifically

CI/CD was chosen deliberately over a generic "self-healing agent" demo, for one concrete reason: **it provides a free, real, non-LLM oracle.**

A test suite either passes or it doesn't. That single fact solves the hardest problem in agentic self-repair — verifying that a fix actually worked — without needing another LLM to judge it (which would just reintroduce the same trust problem one level up).

CI/CD failures also naturally bucket into the same three layers described above (flaky test vs. broken build vs. genuine regression), so the problem domain and the architecture map onto each other cleanly. And it's a real, felt, daily pain point for developers — not a synthetic benchmark.

---

## Core Data Model

Every component in the system either reads a `Run` or produces a new `Step`. This is the shared vocabulary the whole system speaks — get this right, and everything else is just functions operating on it.

```
Step = {
  id: unique identifier
  type: "attribution" | "repair" | "verification"
  layer: "mechanical" | "structural" | "semantic"
  input: what went into this step
  output: what came out
  status: pending | running | success | failed | escalated
  attempts: retry count
  cost: { tokens_used, wall_clock_ms }
}

Run = {
  id, repo, failing_commit
  steps: [Step, Step, ...]
  budget: { max_tokens, max_retries_mechanical, max_retries_structural, max_retries_semantic, max_wall_clock }
  spent: { tokens_used, retries_used_mechanical, retries_used_structural, retries_used_semantic, time_used }
  final_status: healed | escalated | failed
}
```

---

## Full Pipeline Flow

```
CI Failure (webhook)
      │
      ▼
[Ingestion Layer] ──── [Rate/Anomaly Guard]
      │
      ▼
[Input Guardrail]   (scans raw logs/diffs for injection before any LLM sees them)
      │
      ▼
[Classifier]   → mechanical / structural / semantic
      │
      ▼
[Attribution Engine]   → root cause + structured evidence (not a score)
      │
      ▼
[Repair Router]   → picks handler + model tier, deterministically
      │
      ├──► [Mechanical Handler]   (no model)
      ├──► [Structural Handler]   (SLM by default, LLM if large/complex)
      └──► [Semantic Handler]     (LLM, always)
                  │
                  ▼
           [Output Guardrail]
                  │
                  ▼
           [Confidence Gate]   (checks evidence structure, not a threshold)
                  │
                  ▼
           [Scope Guard]   (fixed rules: file-count limits, no self-modifying CI config)
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
   [Verifier]          [Escalation]
   (real test suite,        │
   only thing that      Human gets full trace
   can mark success)    (append-only audit log)
        │
        ▼
   [Action Layer]   → opens PR, only after verified pass + scope check
   [Kill Switch]     → human-controlled, overridable by nothing else
```

---

## Component Reference

### Ingestion Layer
Receives CI failure events (GitHub webhook or similar), deduplicates retried deliveries, and creates a new `Run`. Sits behind a queue so bursts of simultaneous failures don't overwhelm downstream processing.

### Rate/Anomaly Guard
Tracks event frequency per actor/repo. Throttles abnormal bursts before they ever reach the Classifier — protects the system from being flooded or having its budget attacked at the volume level, which per-Run budget checks alone can't catch.

### Input Guardrail *(adopted, not custom-built)*
Scans all externally-sourced content — logs, diffs, commit messages, code comments — before it's included in any LLM prompt. Defends against prompt injection: a malicious comment or log string attempting to hijack the model's instructions. Uses an existing open-source scanner (e.g. LLM Guard or Llama Guard) rather than reinventing prompt-injection detection.

### Classifier
A deterministic, rule-based function — no LLM. Labels each failure as mechanical, structural, or semantic based on exit codes and log patterns. This is the component that makes the entire cost-saving architecture work: if it mislabels things, everything downstream loses its efficiency, so it's treated as safety-critical despite being the simplest piece.

### Attribution Engine
For semantic failures, determines the specific root cause using **counterfactual, intervention-based reasoning** — testing whether undoing the suspected cause actually flips the outcome — rather than an LLM guessing from a diff. Produces a structured explanation:

```
Attribution = {
  claimed_cause,
  evidence_for: [...],
  evidence_against: [...],
  alternatives_considered: [{ cause, why_rejected }, ...],
  counterfactual_result: "pass" | "fail" | "inconclusive"
}
```
No single float confidence score — deliberately, because a bare number can be reported with false precision and gives nothing to audit.

### Repair Router
A deterministic dispatch function. Given the classification and attribution, selects which handler runs and which model tier it uses. Never an LLM itself — routing is a lookup, not a reasoning task.

### Mechanical Handler
Retry with backoff, or deterministic rerouting around a known-equivalent tool/endpoint. No model involved.

### Structural Handler
Calls a configured model provider to correct malformed config/output. User chooses which provider in `.env`:
- Default: Groq (fast, cost-effective)
- Alternative: OpenAI, Anthropic, OpenRouter
- Fixed temperature (0.0) for deterministic fixes

### Semantic Handler
Calls a configured model provider to propose code fixes for logic failures. User chooses which provider in `.env`:
- Default: Groq (recommended for testing)
- Production: OpenAI GPT-4 (highest quality), Anthropic Claude (strong reasoning)
- Temperature: 0.7 for balanced exploration

**Key insight**: User decides which model for which task. Not locked to "SLM for structural, LLM for semantic." You can use Groq for both, or OpenAI for semantic + Groq for structural, or any other combination.

### Output Guardrail *(adopted)*
Scans generated fixes before they reach the Verifier — for leaked secrets, unsafe patterns, or content that looks like it's weakening the test suite itself rather than fixing the underlying bug.

### Confidence Gate
Decides whether a proposed fix is eligible for automatic application, based on the *structure* of the Attribution's evidence — not a threshold on a number:
```
auto_apply_eligible = (
  counterfactual_result == "pass"
  AND evidence_against is empty
  AND all alternatives were explicitly rejected with a stated reason
)
```

### Scope Guard
Hard, fixed safety rules — not confidence-based. Enforces two separate concerns:

**Numeric Limits (Configurable per repo/org):**
- Max files touched (default: 5, configurable via `ScopeConfig`)
- Max lines changed (default: 100, configurable via `ScopeConfig`)
- Falls back to safe defaults if no config exists (never fails open)

**Protected File Patterns (Hardcoded, not configurable alongside limits):**
- CI configuration files (`.github/workflows`, `.gitlab-ci.yml`, Jenkinsfile, etc.)
- Test files (`test_*.py`, `*.test.js`, etc.)
- Security-sensitive files (`security.py`, `auth.py`, `crypto.py`, etc.)
- Build and dependency config (`setup.py`, `package.json`, `docker-compose.yml`, etc.)

Protected paths can only be overridden with an explicit, separately-named flag (`allow_protected_paths_override`) that requires substantive justification in `protected_paths_override_reason`. This separation prevents casual loosening of safety limits. A patch modifying protected files is automatically excluded from auto-apply regardless of confidence, unless override is explicitly enabled by a developer.

**Important: Access Control for Configuration**

`ScopeConfigManager.set_config()` has **no built-in access control** — it is a data structure with no enforcement boundary. Deployers are responsible for gating calls to `set_config()` behind an admin-only path:
- Configuration file checked into version control (with code review)
- Admin-only API endpoint (authentication + authorization required)
- Manual approval process (separate from the pipeline)

If a compromised CI job, untrusted Skill, or malicious handler can reach `set_config()` directly, it can weaken scope limits or enable protected-paths override. This is **by design** — the safety boundary is external, not internal — but it means deployment security depends entirely on the deployer correctly isolating configuration changes outside the pipeline execution path.

### Verifier
Runs the real test suite in an isolated sandbox. Returns pass/fail — nothing else. **This is the only component in the entire system permitted to mark a Step as `success`.** No handler, model, or confidence score can self-certify.

### Action Layer
The only component allowed to touch the real repository. Opens a PR only after verification passes and Scope Guard clears it. If the Run exhausts its budget or fails the Confidence Gate, this component posts a clear, human-readable escalation with the full trace of what was tried and why.

### Budget Guard
Tracks tokens, retries, and wall-clock time spent across a Run, atomically, in one centralized place — preventing budget-leak bugs that would occur if every handler tracked its own spend independently, and preventing race conditions across parallel branches.

### Priority Queue
Determines which queued failure gets worked on first. **Decided by the developer**, not computed by the system — the system surfaces plain-language context (wait time, what's blocked, attribution summary) so the decision is informed, but the ordering itself is a human call, because only a developer has the real-world context (what's urgent, what's blocking a release) that the system cannot see.

### Kill Switch
A human-controlled flag that disables auto-apply entirely for a repo or org. No Skill, handler, or Confidence Gate result can override it. The guardrail of last resort — doesn't depend on any other component's logic being correct.

### Audit Log
An append-only, tamper-evident record of every decision — what was diagnosed, what was tried, what was auto-applied or escalated, and why. Makes every other guardrail's decision reviewable after the fact.

---

## Guardrails

| Guardrail | Adopt or Design | Purpose |
|---|---|---|
| Input/Output scanning (injection, secrets, PII) | **Adopt** — LLM Guard / Llama Guard | Untrusted repo content never silently becomes instructions to the model |
| Rate/Anomaly Guard | Design | Prevents volume-based budget attacks |
| Scope Guard | Design | Fixed blast-radius limits on auto-applied patches |
| Kill Switch | Design | Human override that bypasses nothing and is bypassed by nothing |
| Audit Log immutability | Design | Tamper-evident record for review and trust |

The generic "is this content trying to manipulate the model" problem is solved, mature tooling — adopted, not reinvented. The rules specific to Kintsugi's own blast radius (file limits, CI-config exclusion, kill switch) are designed in-house because no generic library knows this system's action space.

---

## Redis — Where and Why

Redis is used only where the actual requirement is **shared, atomic, cross-process state** — not by default.

| Use | Why Redis fits |
|---|---|
| Ingestion queue | Streams handle ordered, bursty delivery + consumer groups; dedup via `SETNX` on webhook ID |
| Budget Guard | `INCR`/`DECRBY` are atomic — solves the cross-branch race condition directly |
| Rate Guard | Sorted-set sliding window is the standard pattern for this exact problem |
| Run/Step state | Shared Hash means any worker can resume a Run after a crash — a Python dict in one process's memory cannot |
| Priority Queue | Sorted Set with developer-assigned priority as score — reorder and read are both cheap |
| Kill Switch | Single key, instantly visible across every worker process |
| Fix-cache (hot/cold tiers) | Sorted Set (hot) + Hash (cold) with TTL — matches spill-not-discard tiering (deferred until real usage data justifies building it) |

**Not used for:** the audit log. Redis isn't built for durable, queryable, long-term history — that requirement belongs to a real datastore (Postgres or structured files), a different requirement than Redis serves.

---

## Per-Layer Retry Budgets

Kintsugi uses per-layer retry budgets to prevent cheap operations from exhausting the budget of expensive ones. Each layer has its own retry limit:

- **Mechanical retries** (default: 5) — Cheap, no LLM calls. Retry/backoff logic.
- **Structural retries** (default: 3) — Moderate cost. Uses SLM for config/format fixes.
- **Semantic retries** (default: 2) — Expensive. Uses LLM for logic fixes.

This prevents a situation where trivial mechanical retries consume all the retry budget, leaving semantic repairs unable to attempt fixes for genuinely complex issues.

Configure in `.env`:
```bash
MAX_RETRIES_MECHANICAL=5    # Mechanical layer: up to 5 retries
MAX_RETRIES_STRUCTURAL=3    # Structural layer: up to 3 retries
MAX_RETRIES_SEMANTIC=2      # Semantic layer: up to 2 retries
MAX_RETRIES_TOTAL=7         # Hard outer ceiling: tighter than sum (5+3+2=10)
```

**Why MAX_RETRIES_TOTAL is set to 7, not 10**: The total is deliberately *tighter* than the sum of per-layer maximums. This forces escalation if a Run churns through multiple layers even if no single layer is exhausted. Example: a Run could do 3 mechanical + 2 structural + 2 semantic = 7 total retries and hit the ceiling despite each layer respecting its own budget. This prevents pathological retry patterns that would be individually acceptable but collectively wasteful.

**Per-layer enforcement is primary** (mechanical, structural, semantic checked first).
**Total ceiling is secondary safety net** (checked before per-layer limits when evaluating budget exhaustion, like how max_tokens caps the whole Run regardless of spend distribution).

Error messages are distinct:
- `"Overall retry budget exhausted: 7/7 total retries"` — ceiling hit
- `"Semantic retry budget exhausted: 2/2"` — layer-specific limit hit

Each layer's retry count is tracked independently in `Run.spent`:
- `spent.retries_used_mechanical` — Mechanical retries consumed
- `spent.retries_used_structural` — Structural retries consumed
- `spent.retries_used_semantic` — Semantic retries consumed

When any limit is hit (per-layer or total), repair handlers are disabled for the run, but other layers can continue using their full budgets until their limits are reached.

---

## Model Provider Configuration

**Kintsugi now supports real LLM/SLM providers.** You choose which models to use in `.env`:

### Supported Providers

- **Groq** (Mixtral 8x7B) — Fast, cost-effective (~$0.00024/1k tokens) — **recommended for testing**
- **OpenAI** (GPT-4, GPT-3.5) — High quality reasoning (~$0.003/1k tokens)
- **Anthropic** (Claude) — Excellent analysis (~$0.015/1k tokens)
- **OpenRouter** — Many models, fallback support

### Quick Start

```bash
# 1. Copy .env.example to .env
cp .env.example .env

# 2. Choose providers and add API keys (example):
SEMANTIC_PROVIDER=groq
STRUCTURAL_PROVIDER=groq
GROQ_API_KEY=gsk_your_key_here

# 3. Pipeline automatically uses these
python -c "from src.pipeline import KintsugiPipeline; p = KintsugiPipeline()"
```

### Configuration Examples

**Testing (Groq - cheapest):**
```bash
SEMANTIC_PROVIDER=groq
STRUCTURAL_PROVIDER=groq
GROQ_API_KEY=gsk_...
```

**Production (Best quality):**
```bash
SEMANTIC_PROVIDER=openai
STRUCTURAL_PROVIDER=groq
OPENAI_API_KEY=sk_...
GROQ_API_KEY=gsk_...
```

**Maximum Quality:**
```bash
SEMANTIC_PROVIDER=anthropic
STRUCTURAL_PROVIDER=anthropic
ANTHROPIC_API_KEY=claude_...
```

### Features

- **Flexible**: User chooses provider for each task
- **Automatic Retry**: Exponential backoff on rate limits, timeouts, errors
- **Cost Tracking**: Token usage and cost logged per call
- **No Secrets in Code**: All API keys in `.env` file
- **CI/CD Ready**: Requires Groq API key (cheap, reliable, fast)

See `MODEL_CONFIGURATION.md` for detailed setup and cost comparison.

---

## Skills (Extension Layer)

A Skill is a domain-specific bundle supplying:
- Classifier rules for a specific stack/framework
- Handler logic for that domain's failure patterns
- A Verifier definition (what "correct" means for that domain)

**A Skill supplies domain knowledge. It never becomes the orchestrator.** It plugs into the same fixed core via a pure-function contract — `(event, state) → (action, new_state)` — and every Skill's input and output passes through the same Input/Output Guardrails, Confidence Gate, and Scope Guard as built-in handlers. A Skill's self-reported confidence never bypasses the core's trust decisions; the core always decides whether to act, the Skill only decides what to try.

This is what makes it safe to eventually let any developer write and share Skills without needing to personally audit each one — the safety guarantees live in the fixed core, not in each Skill's own code.

---

## Developer Deployment Segments

Kintsugi uses a flexible model provider interface. Choose which provider to use for each task in `.env`:

```bash
SEMANTIC_PROVIDER=groq           # or: openai, anthropic, openrouter
STRUCTURAL_PROVIDER=groq         # or: openai, anthropic, openrouter
GROQ_API_KEY=gsk_...
```

**Provider Options:**
- **Groq**: Fast, cost-effective — recommended for testing
- **OpenAI**: High quality GPT-4 — recommended for production semantic repairs
- **Anthropic**: Strong reasoning Claude — alternative for complex analysis
- **OpenRouter**: Many models, fallback support

**Features:**
- Automatic retry with exponential backoff
- Token usage and cost tracking
- All API keys in `.env`, not in code
- CI/CD uses Groq (fast, cheap, reliable)

See `MODEL_CONFIGURATION.md` for complete setup guide.

---

## Design Principles

1. **Deterministic wherever possible; model only where reasoning is genuinely required.**
2. **Explainable over scored** — attribution and confidence are structured evidence, not a self-reported float.
3. **Only the Verifier can claim success** — no component may self-certify.
4. **Humans decide what only humans can know** — priority ordering is a human call, supported by system-generated context, not replaced by it.
5. **Fixed safety boundaries are rules, not confidence judgments** — Scope Guard's limits apply regardless of stated confidence.
6. **Guardrails sit at trust boundaries** — where untrusted content enters, and where actions leave the sandbox.
7. **Every failure to auto-resolve is explained, never hidden** — the same philosophy the project is named for.

---

## What's Deferred and Why

These are real, designed pieces of the architecture — deferred in build order, not cut from the design:

- **Skills marketplace** — the extension contract is designed now; opening it to arbitrary third-party authorship comes after the core pyramid is proven on real failures.
- **Fix-cache** — build once real repeat-failure data exists to justify it; premature caching optimizes a system that hasn't been proven yet.
- **Context-bucket tiering for semantic retries** — build once measured context bloat on real retries justifies it.
- **Self-hosted / local full support** — provider interface supports it; implementation deferred until a real user in that segment exists.

---

*Kintsugi: repairs that are visible, explainable, and honest about their own uncertainty — never silently patched over.*
