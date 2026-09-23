"""Context-Bucket Tiering for semantic retries (v2).

v1 fed the semantic handler the same everything-at-once prompt on every
attempt. v2 gives each semantic attempt a *bucket*: a bounded, prioritized
slice of context that widens only when the narrower slice has already failed.

  Bucket 1  cheapest, most targeted
            - the seed: a NEAR-match cached Attribution (if the fix-cache found one)
            - the current Attribution's own evidence (cause, for, against)
            - a focused excerpt of the failure log around the first error
  Bucket 2  bucket 1 + broader code context
            - the changed hunks (recent diff)
            - surrounding code (adjacent functions), if a provider supplies it
  Bucket 3  full context — expensive fallback
            - full logs, full diff, commit message, alternatives considered

Attempt N (0-based) uses bucket min(N + 1, 3). The semantic retry budget
(MAX_RETRIES_SEMANTIC, default 2) is not changed by this module, so with the
defaults only buckets 1 and 2 are ever used; bucket 3 is reached only if an
operator raises the semantic budget to 3+.

Each bucket has a character budget (a proxy for tokens: ~4 chars/token).
Sections are added in priority order; the section that crosses the budget is
truncated and anything after it is dropped, and the bucket records that it was
truncated so the audit trail shows the model did not see everything.

Surrounding code: Kintsugi only receives the diff and logs from the webhook,
not a checkout. Pass a ``code_context_provider(run) -> str`` to
ContextBucketBuilder to supply adjacent functions from a real checkout; without
one, ``run.metadata["surrounding_code"]`` is used if present, and otherwise the
section is simply omitted (bucket 2 then adds only the diff).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.models import Attribution, Run


# Character budgets per tier (None = unbounded). ~4 chars per token, so
# 6000 chars ~ 1.5k tokens and 14000 chars ~ 3.5k tokens.
DEFAULT_CHAR_BUDGETS: Dict[int, Optional[int]] = {1: 6000, 2: 14000, 3: None}

_TRUNCATION_MARK = "\n[... truncated to fit context bucket ...]"
_ERROR_LINE_RE = re.compile(r"(error|exception|assert|failed|traceback|expected)", re.IGNORECASE)


def bucket_tier_for_attempt(attempt_index: int) -> int:
    """0 -> 1, 1 -> 2, 2+ -> 3."""
    return min(max(attempt_index, 0) + 1, 3)


def focused_log_excerpt(logs: str, context_lines: int = 3, max_lines: int = 40) -> str:
    """Lines around error-looking lines, instead of the whole log.

    Falls back to the log's tail when nothing looks like an error.
    """
    lines = (logs or "").splitlines()
    if not lines:
        return ""
    keep: List[int] = []
    for i, line in enumerate(lines):
        if _ERROR_LINE_RE.search(line):
            keep.extend(range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)))
    if not keep:
        return "\n".join(lines[-max_lines:])
    ordered = sorted(set(keep))[:max_lines]
    out: List[str] = []
    prev = None
    for idx in ordered:
        if prev is not None and idx != prev + 1:
            out.append("...")
        out.append(lines[idx])
        prev = idx
    return "\n".join(out)


def _bullets(items: Optional[List[str]]) -> str:
    items = [i for i in (items or []) if i]
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


def _format_attribution(attr: Attribution, include_alternatives: bool = False) -> str:
    parts = [
        f"Claimed cause: {attr.claimed_cause}",
        f"Counterfactual result: {attr.counterfactual_result or 'inconclusive'}",
        f"Evidence for:\n{_bullets(attr.evidence_for)}",
        f"Evidence against:\n{_bullets(attr.evidence_against)}",
    ]
    if include_alternatives and attr.alternatives_considered:
        alts = "\n".join(
            f"- {a.get('cause', '?')} (rejected: {a.get('why_rejected', 'no reason given')})"
            for a in attr.alternatives_considered
        )
        parts.append(f"Alternatives considered:\n{alts}")
    return "\n".join(parts)


@dataclass
class ContextBucket:
    """One bounded slice of context for one semantic attempt."""
    tier: int
    sections: List[Tuple[str, str]] = field(default_factory=list)
    char_budget: Optional[int] = None
    truncated: bool = False
    dropped_sections: List[str] = field(default_factory=list)
    seeded: bool = False
    seed_fingerprint: Optional[str] = None
    seed_score: Optional[float] = None

    def render(self) -> str:
        return "\n\n".join(f"### {title}\n{body}" for title, body in self.sections)

    @property
    def char_count(self) -> int:
        return len(self.render())

    @property
    def approx_tokens(self) -> int:
        return self.char_count // 4

    def summary(self) -> Dict[str, Any]:
        """Small dict for Step.input / the audit log (no content)."""
        return {
            "tier": self.tier,
            "sections": [t for t, _ in self.sections],
            "chars": self.char_count,
            "approx_tokens": self.approx_tokens,
            "char_budget": self.char_budget,
            "truncated": self.truncated,
            "dropped_sections": list(self.dropped_sections),
            "seeded": self.seeded,
            "seed_fingerprint": self.seed_fingerprint,
            "seed_score": self.seed_score,
        }


class ContextBucketBuilder:
    """Builds bucket 1/2/3 for a Run. Deterministic; no model calls."""

    def __init__(
        self,
        char_budgets: Optional[Dict[int, Optional[int]]] = None,
        code_context_provider: Optional[Callable[[Run], Optional[str]]] = None,
    ):
        self.char_budgets = dict(DEFAULT_CHAR_BUDGETS)
        if char_budgets:
            self.char_budgets.update(char_budgets)
        self.code_context_provider = code_context_provider

    def _surrounding_code(self, run: Run) -> Optional[str]:
        if self.code_context_provider is not None:
            try:
                code = self.code_context_provider(run)
            except Exception:  # a context helper must never break a repair
                code = None
            if code:
                return code
        code = (run.metadata or {}).get("surrounding_code")
        return code or None

    def build(
        self,
        tier: int,
        run: Run,
        attribution: Attribution,
        seed_attribution: Optional[Attribution] = None,
        seed_fingerprint: Optional[str] = None,
        seed_score: Optional[float] = None,
    ) -> ContextBucket:
        if tier not in (1, 2, 3):
            raise ValueError(f"bucket tier must be 1, 2 or 3, got {tier}")

        # Ordered by priority: the most targeted material first so that, if the
        # budget cuts, it cuts the broad material.
        sections: List[Tuple[str, str]] = []
        if seed_attribution is not None:
            header = "Prior verified diagnosis of a similar failure (seed — may not apply exactly)"
            if seed_score is not None:
                header += f", similarity {seed_score:.2f}"
            sections.append((header, _format_attribution(seed_attribution)))
        sections.append(("Current diagnosis", _format_attribution(attribution, include_alternatives=(tier == 3))))

        if tier < 3:
            sections.append(("Failure log (focused excerpt)", focused_log_excerpt(run.failure_logs)))
        else:
            sections.append(("Failure log (full)", run.failure_logs or "(empty)"))

        if tier >= 2:
            sections.append(("Recent change (diff)", run.diff or "(empty diff)"))
            code = self._surrounding_code(run)
            if code:
                sections.append(("Surrounding code", code))

        if tier == 3 and run.commit_message:
            sections.append(("Commit message", run.commit_message))

        bucket = ContextBucket(
            tier=tier,
            char_budget=self.char_budgets.get(tier),
            seeded=seed_attribution is not None,
            seed_fingerprint=seed_fingerprint,
            seed_score=seed_score,
        )
        self._fill(bucket, sections)
        return bucket

    @staticmethod
    def _fill(bucket: ContextBucket, sections: List[Tuple[str, str]]) -> None:
        # AX-VERIFIED: a bounded bucket's rendered size never exceeds its
        # char_budget (whenever the budget is at least as long as the first
        # section's "### title" line), and the first (highest-priority) section
        # is always kept, hard-cut if necessary.
        # (tests/test_context_buckets.py::TestBudgets::test_rendered_size_within_budget,
        # ::test_first_section_survives_tiny_budget)
        budget = bucket.char_budget
        for i, (title, body) in enumerate(sections):
            if budget is None:
                bucket.sections.append((title, body))
                continue
            candidate = bucket.sections + [(title, body)]
            rendered_len = len("\n\n".join(f"### {t}\n{b}" for t, b in candidate))
            if rendered_len <= budget:
                bucket.sections.append((title, body))
                continue
            # This section crosses the budget: truncate it, drop the rest.
            used = len("\n\n".join(f"### {t}\n{b}" for t, b in bucket.sections))
            sep = 2 if bucket.sections else 0
            room = budget - used - sep - len(f"### {title}\n") - len(_TRUNCATION_MARK)
            if room > 0:
                bucket.sections.append((title, body[:room] + _TRUNCATION_MARK))
            elif not bucket.sections:
                # Budget too small for even a truncated first section: keep a
                # hard-cut version so the attempt still has its core diagnosis.
                bucket.sections.append((title, (f"{body}")[: max(budget - len(f'### {title}\n'), 0)]))
            else:
                bucket.dropped_sections.append(title)
            bucket.truncated = True
            bucket.dropped_sections.extend(t for t, _ in sections[i + 1:])
            return


_HUNK_NEW_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_NEW_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(\S+)", re.MULTILINE)


def checkout_code_context(sandbox, window: int = 25, max_chars: int = 8000) -> Callable[[Run], Optional[str]]:
    """
    A `code_context_provider` that reads the real code around each changed hunk.
    
    For every file in the Run's diff it reads the file at the failing commit
    from the configured checkout (`RepoSandbox.show_file`) and returns the
    lines around each hunk, with line numbers, so the model sees actual code
    (and real line numbers for its @@ headers) instead of guessing names.
    Returns None when the repo has no checkout or the diff has no hunks.
    """
    def provider(run: Run) -> Optional[str]:
        if not getattr(sandbox, "is_configured", None) or not sandbox.is_configured(run.repo):
            return None
        blocks: List[str] = []
        sections = re.split(r"(?=^--- )", run.diff or "", flags=re.MULTILINE)
        for section in sections:
            m = _NEW_FILE_RE.search(section)
            if not m or m.group(1) == "/dev/null":
                continue
            path = m.group(1)
            text = sandbox.show_file(run.repo, run.failing_commit, path)
            if text is None:
                continue
            lines = text.splitlines()
            ranges: List[Tuple[int, int]] = []
            for h in _HUNK_NEW_RE.finditer(section):
                start, count = int(h.group(1)), int(h.group(2) or 1)
                ranges.append((max(1, start - window), min(len(lines), start + count + window)))
            ranges.sort()
            merged: List[Tuple[int, int]] = []
            for a, b in ranges:
                if merged and a <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            for a, b in merged:
                body = "\n".join(f"{n:>5} | {lines[n - 1]}" for n in range(a, b + 1))
                blocks.append(f"{path} (lines {a}-{b} at the failing commit)\n{body}")
        if not blocks:
            return None
        out = "\n\n".join(blocks)
        return out[:max_chars]
    
    return provider
