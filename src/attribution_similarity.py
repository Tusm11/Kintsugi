"""Attribution Similarity: fingerprint and compare Attribution records (v2 core).

Everything in the v2 fix-cache and context-bucket work depends on answering one
question: "is this failure's root cause the same as one we already fixed?"

Exact text match is useless for that — two occurrences of the same root cause
differ in line numbers, variable names, file paths and the model's phrasing.
So an Attribution is reduced to its *stable parts* before comparison:

  1. claimed_cause   -> a normalized structural pattern (identifiers, numbers,
                        paths and quoted literals replaced by placeholders)
  2. evidence_for    -> a *shape*: how many real items, and which categories
  3. evidence_against-> same shape treatment
  4. counterfactual_result -> compared as-is

Similarity is a weighted match across those four fields, never a raw text diff.
The result is bucketed into three tiers:

  EXACT -> identical fingerprint. Safe to reuse the cached fix directly
           (it is still re-verified downstream — see fix_cache.py).
  NEAR  -> similar enough to use the cached Attribution as seed context for a
           fresh generation, never similar enough to reuse the patch.
  NONE  -> cold start.

This module is pure: no I/O, no model calls, no Redis. That is deliberate — it is
the highest-risk piece of v2 (a bad similarity function makes both the cache and
the buckets *worse than doing nothing*) so it must be testable in isolation
against past Attribution records. See `calibration_report()`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.models import Attribution


# ---------------------------------------------------------------------------
# Tiers and tunables
# ---------------------------------------------------------------------------

class MatchTier(str, Enum):
    """How a cached attribution relates to the current one."""
    EXACT = "exact"
    NEAR = "near"
    NONE = "none"


# Field weights for the similarity score. They sum to 1.0.
# claimed_cause dominates because it is the only field that actually names the
# root cause; evidence shape and counterfactual are corroborating signals.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "cause": 0.55,
    "evidence_for": 0.20,
    "evidence_against": 0.10,
    "counterfactual": 0.15,
}

# Overall score needed for NEAR.
DEFAULT_NEAR_THRESHOLD = 0.65

# NEAR additionally requires the *cause* alone to clear this bar, so that two
# unrelated failures whose evidence happens to look alike (very common: most
# model outputs have 3-5 "diff"/"assertion" items) can never be called near.
DEFAULT_MIN_CAUSE_SIMILARITY = 0.5

# Bump when normalization/categorization rules change: fingerprints produced by
# different rule versions must never compare equal.
FINGERPRINT_VERSION = 1


# ---------------------------------------------------------------------------
# claimed_cause normalization
# ---------------------------------------------------------------------------

# Leading labels the prompts ask the model to emit ("ROOT_CAUSE: ...") and list
# markers ("1.", "-", "*").
_LEADING_LABEL_RE = re.compile(r"^\s*(?:[A-Z][A-Z_ ]{2,30}:\s*)?(?:[-*•]|\d+[.)])?\s*")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_PATH_RE = re.compile(
    r"(?:[\w.-]+[/\\])+[\w.-]+"                               # a/b/c.py, src\x.py
    r"|\b[\w-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|rb|c|cc|cpp|h|hpp|cs|yml|yaml|json|toml|cfg|ini|md)\b"
)
_LINE_REF_RE = re.compile(r"\b(line|lines|col|column)\s+\d+(?:\s*[-:]\s*\d+)?", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b\d+(?:\.\d+)?\b")
# Identifier-like tokens: snake_case, camelCase/PascalCase with an inner capital,
# dotted names, or anything immediately followed by "(" (a call).
_IDENT_RE = re.compile(
    r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b"      # dotted: self.total, os.path.join
    r"|\b[A-Za-z]*_[A-Za-z0-9_]*\b"              # snake_case / _private
    r"|\b[a-z]+[A-Z]\w*\b"                       # camelCase
    r"|\b[A-Z][a-z0-9]+[A-Z]\w*\b"               # PascalCase (e.g. OrderTotal)
    r"|\b[A-Za-z_]\w*(?=\()"                     # foo(
)
_TOKEN_RE = re.compile(r"<[a-z]+>|[a-z][a-z0-9]*")

# Placeholders. Exception class names are *kept* (they are structural: a
# KeyError and a TypeError are different root causes), everything else that is
# instance-specific collapses.
_EXCEPTION_NAME_RE = re.compile(r"\b[A-Z]\w*(?:Error|Exception|Warning)\b")

_STOPWORDS = frozenset(
    """a an the of in on at to for from by with and or but is are was were be been
    being this that these those it its as into than then so such which who whom
    whose what when where why how there here has have had do does did not no can
    could would should may might will shall very more most less least also just
    only some any each all both either neither due because caused cause causes
    likely probably possibly appears seems suspected root issue problem""".split()
)


def _light_stem(token: str) -> str:
    """Tiny suffix stripper so 'removed'/'removes'/'removing' collapse together."""
    if token.startswith("<") or len(token) <= 4:
        return token
    for suffix in ("ing", "ed", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def normalize_cause(cause: Optional[str]) -> List[str]:
    """Reduce a claimed_cause to an ordered list of structural tokens.

    Instance-specific details are replaced with placeholders
    (<str>, <path>, <num>, <id>); exception class names are kept (lower-cased);
    stopwords are dropped; consecutive duplicate placeholders are collapsed.

    >>> normalize_cause("Off-by-one in calculate_total() at line 42 of src/cart.py")
    ['off', 'one', '<id>', '<path>']
    """
    if not cause:
        return []

    text = _LEADING_LABEL_RE.sub("", cause.strip(), count=1)

    # Protect exception names before the identifier pass eats PascalCase ones.
    exceptions: List[str] = []

    def _keep_exception(match: "re.Match[str]") -> str:
        exceptions.append(match.group(0).lower())
        return f" exc{len(exceptions) - 1}marker "

    text = _EXCEPTION_NAME_RE.sub(_keep_exception, text)
    text = _QUOTED_RE.sub(" <str> ", text)
    text = _PATH_RE.sub(" <path> ", text)
    text = _LINE_REF_RE.sub(" ", text)          # "line 42" carries no structure
    text = _IDENT_RE.sub(" <id> ", text)
    text = _NUMBER_RE.sub(" <num> ", text)

    tokens: List[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        marker = re.fullmatch(r"exc(\d+)marker", raw)
        if marker:
            tok = exceptions[int(marker.group(1))]
        elif raw in _STOPWORDS:
            continue
        else:
            tok = _light_stem(raw)
        if tok.startswith("<") and tokens and tokens[-1] == tok:
            continue  # collapse "<id> <id>" runs
        tokens.append(tok)
    return tokens


def cause_similarity(a_tokens: Sequence[str], b_tokens: Sequence[str]) -> float:
    """Jaccard similarity over normalized cause token sets (0.0 - 1.0).

    Placeholders count as tokens (an "<id>" on both sides is weak evidence of
    shared structure) but two causes consisting *only* of placeholders are
    treated as uninformative and score 0.
    """
    a, b = set(a_tokens), set(b_tokens)
    if not a and not b:
        return 0.0
    informative = {t for t in (a | b) if not t.startswith("<")}
    if not informative:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Evidence shape
# ---------------------------------------------------------------------------

# Ordered: the first matching rule wins, so more specific categories go first.
_EVIDENCE_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # Filler strings the AttributionEngine itself emits when it found nothing.
    # These are not evidence and must not count toward the shape.
    ("placeholder", re.compile(
        r"^(no (supporting|contradicting) evidence found|limited (counter-)?evidence"
        r"|heuristic analysis inconclusive|model could not identify|none\.?$|n/?a$)",
        re.IGNORECASE)),
    ("counterfactual", re.compile(r"revert|undo|counterfactual|would (pass|fail)|if .* (restored|removed)", re.I)),
    ("boundary", re.compile(r"off[- ]by[- ]one|index|out of range|bound|length|\blen\b|fencepost", re.I)),
    ("type", re.compile(r"\btype\b|typeerror|attributeerror|none(type)?\b|null|undefined|cast", re.I)),
    ("assertion", re.compile(r"assert|expected|actual|\bgot\b|mismatch|differs?", re.I)),
    ("log", re.compile(r"traceback|stack ?trace|\blogs?\b|exception|raised|error message", re.I)),
    ("control_flow", re.compile(r"\breturn|condition|branch|\bloop|\bif\b|\belse\b|early exit|short[- ]circuit", re.I)),
    ("state", re.compile(r"\bstate\b|mutat|global|cache|side[- ]effect|order(ing)?\b|race", re.I)),
    ("diff", re.compile(r"\bdiff\b|commit|chang|remov|add(ed|s)?\b|modif|introduc", re.I)),
)


def categorize_evidence(item: str) -> str:
    """Assign one evidence string to a single coarse category."""
    text = (item or "").strip()
    if not text:
        return "placeholder"
    for name, pattern in _EVIDENCE_RULES:
        if pattern.search(text):
            return name
    return "other"


@dataclass(frozen=True)
class EvidenceShape:
    """Count + category mix of an evidence list. Exact text is discarded."""
    count: int
    categories: Tuple[Tuple[str, int], ...]  # sorted (category, n) pairs

    @property
    def count_bucket(self) -> str:
        """Coarse count used in the fingerprint (model output length is noisy)."""
        if self.count == 0:
            return "0"
        if self.count == 1:
            return "1"
        if self.count <= 3:
            return "2-3"
        return "4+"

    def as_counter(self) -> Counter:
        return Counter(dict(self.categories))

    def to_dict(self) -> Dict[str, Any]:
        return {"count": self.count, "categories": dict(self.categories)}


def evidence_shape(items: Optional[Iterable[str]]) -> EvidenceShape:
    """Build an EvidenceShape, ignoring placeholder/filler items entirely."""
    counts: Counter = Counter()
    for item in items or []:
        category = categorize_evidence(item)
        if category != "placeholder":
            counts[category] += 1
    return EvidenceShape(
        count=sum(counts.values()),
        categories=tuple(sorted(counts.items())),
    )


def shape_similarity(a: EvidenceShape, b: EvidenceShape) -> float:
    """Weighted-Jaccard over category counts, blended with count closeness."""
    if a.count == 0 and b.count == 0:
        return 1.0
    ca, cb = a.as_counter(), b.as_counter()
    keys = set(ca) | set(cb)
    overlap = sum(min(ca[k], cb[k]) for k in keys)
    union = sum(max(ca[k], cb[k]) for k in keys)
    category_sim = overlap / union if union else 0.0
    count_sim = 1.0 - abs(a.count - b.count) / max(a.count, b.count)
    return 0.7 * category_sim + 0.3 * count_sim


def counterfactual_similarity(a: Optional[str], b: Optional[str]) -> float:
    """1.0 if equal, 0.25 if either side is inconclusive/missing, 0.0 if opposite."""
    a_norm = (a or "inconclusive").lower()
    b_norm = (b or "inconclusive").lower()
    if a_norm == b_norm:
        return 1.0
    if "inconclusive" in (a_norm, b_norm):
        return 0.25
    return 0.0


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AttributionFeatures:
    """The stable parts of an Attribution, pre-computed once for comparison."""
    cause_tokens: Tuple[str, ...]
    evidence_for: EvidenceShape
    evidence_against: EvidenceShape
    counterfactual: str
    attribution_source: str = "model"

    @property
    def fingerprint(self) -> str:
        """Hex digest of the canonical stable parts.

        Two Attributions share a fingerprint iff they have the same normalized
        cause pattern, the same evidence category *sets* and count buckets on
        both sides, and the same counterfactual result.
        """
        canonical = {
            "v": FINGERPRINT_VERSION,
            "cause": " ".join(self.cause_tokens),
            "for": [self.evidence_for.count_bucket, sorted(dict(self.evidence_for.categories))],
            "against": [self.evidence_against.count_bucket, sorted(dict(self.evidence_against.categories))],
            "cf": self.counterfactual,
        }
        blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cause_tokens": list(self.cause_tokens),
            "evidence_for": self.evidence_for.to_dict(),
            "evidence_against": self.evidence_against.to_dict(),
            "counterfactual": self.counterfactual,
            "attribution_source": self.attribution_source,
            "fingerprint": self.fingerprint,
        }


def extract_features(attribution: Attribution) -> AttributionFeatures:
    """Reduce an Attribution to the features used by fingerprint + similarity."""
    return AttributionFeatures(
        cause_tokens=tuple(normalize_cause(attribution.claimed_cause)),
        evidence_for=evidence_shape(attribution.evidence_for),
        evidence_against=evidence_shape(attribution.evidence_against),
        counterfactual=(attribution.counterfactual_result or "inconclusive").lower(),
        attribution_source=attribution.attribution_source or "model",
    )


def fingerprint(attribution: Attribution) -> str:
    """Convenience: fingerprint straight from an Attribution."""
    return extract_features(attribution).fingerprint


# ---------------------------------------------------------------------------
# Similarity + tiering
# ---------------------------------------------------------------------------

@dataclass
class SimilarityResult:
    """Outcome of comparing two attributions. `breakdown` is for the audit log."""
    tier: MatchTier
    score: float
    breakdown: Dict[str, float] = field(default_factory=dict)
    reason: str = ""


class AttributionSimilarity:
    """Scores how close two Attribution records are and assigns a MatchTier.

    Tier rules (evaluated in order):
      * EXACT: fingerprints are identical AND the current attribution came from
        real model reasoning (attribution_source == "model"). A
        fallback_heuristic attribution is capped at NEAR, because heuristics are
        exactly the kind of low-information input that collides by accident.
      * NEAR:  score >= near_threshold AND cause similarity alone
        >= min_cause_similarity.
      * NONE:  everything else.
    """

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        near_threshold: float = DEFAULT_NEAR_THRESHOLD,
        min_cause_similarity: float = DEFAULT_MIN_CAUSE_SIMILARITY,
    ):
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"similarity weights must sum to 1.0, got {total}")
        self.near_threshold = near_threshold
        self.min_cause_similarity = min_cause_similarity

    def score(self, current: AttributionFeatures, cached: AttributionFeatures) -> Tuple[float, Dict[str, float]]:
        breakdown = {
            "cause": cause_similarity(current.cause_tokens, cached.cause_tokens),
            "evidence_for": shape_similarity(current.evidence_for, cached.evidence_for),
            "evidence_against": shape_similarity(current.evidence_against, cached.evidence_against),
            "counterfactual": counterfactual_similarity(current.counterfactual, cached.counterfactual),
        }
        total = sum(self.weights[k] * v for k, v in breakdown.items())
        return round(total, 4), {k: round(v, 4) for k, v in breakdown.items()}

    def compare(self, current: AttributionFeatures, cached: AttributionFeatures) -> SimilarityResult:
        # AX-VERIFIED: EXACT is only ever returned for identical fingerprints from a
        # model-sourced current attribution; a fallback_heuristic current attribution
        # can never produce EXACT. (tests/test_attribution_similarity.py::
        # TestTiering::test_exact_requires_identical_fingerprint,
        # ::test_fallback_heuristic_capped_at_near)
        total, breakdown = self.score(current, cached)

        if current.fingerprint == cached.fingerprint:
            if current.attribution_source == "model":
                return SimilarityResult(MatchTier.EXACT, total, breakdown, "identical fingerprint")
            return SimilarityResult(
                MatchTier.NEAR, total, breakdown,
                "identical fingerprint but current attribution is fallback_heuristic; capped at near",
            )

        if total >= self.near_threshold and breakdown["cause"] >= self.min_cause_similarity:
            return SimilarityResult(MatchTier.NEAR, total, breakdown, f"score {total} >= {self.near_threshold}")

        why = (
            f"cause similarity {breakdown['cause']} < {self.min_cause_similarity}"
            if breakdown["cause"] < self.min_cause_similarity
            else f"score {total} < {self.near_threshold}"
        )
        return SimilarityResult(MatchTier.NONE, total, breakdown, why)

    def compare_attributions(self, current: Attribution, cached: Attribution) -> SimilarityResult:
        return self.compare(extract_features(current), extract_features(cached))

    def best_match(
        self, current: AttributionFeatures, candidates: Iterable[Tuple[Any, AttributionFeatures]]
    ) -> Tuple[Optional[Any], SimilarityResult]:
        """Return the (payload, result) with the strongest tier, then highest score.

        `candidates` yields (payload, features) pairs; payload is opaque here
        (the fix-cache passes its entries through).
        """
        rank = {MatchTier.EXACT: 2, MatchTier.NEAR: 1, MatchTier.NONE: 0}
        best_payload: Optional[Any] = None
        best = SimilarityResult(MatchTier.NONE, 0.0, {}, "no candidates")
        for payload, features in candidates:
            result = self.compare(current, features)
            if (rank[result.tier], result.score) > (rank[best.tier], best.score):
                best_payload, best = payload, result
                if result.tier == MatchTier.EXACT:
                    break
        return (best_payload if best.tier != MatchTier.NONE else None), best


# ---------------------------------------------------------------------------
# Offline calibration against past Attribution records
# ---------------------------------------------------------------------------

def calibration_report(
    labeled_pairs: Iterable[Tuple[Attribution, Attribution, bool]],
    similarity: Optional[AttributionSimilarity] = None,
) -> Dict[str, Any]:
    """Measure the similarity function against hand-labeled past records.

    Each item is (attribution_a, attribution_b, same_root_cause). The report
    counts how each tier was assigned for same vs. different pairs. The two
    numbers that matter:

      * ``false_exact``: different root causes scored EXACT. Must be 0 —
        every one of these is a wrong patch the Verifier has to catch.
      * ``false_near``: different root causes scored NEAR. Tolerable in small
        numbers (it only seeds context), but it wastes bucket-1 budget.
    """
    sim = similarity or AttributionSimilarity()
    table = {True: Counter(), False: Counter()}
    for a, b, same in labeled_pairs:
        table[bool(same)][sim.compare_attributions(a, b).tier.value] += 1
    return {
        "same_cause": dict(table[True]),
        "different_cause": dict(table[False]),
        "false_exact": table[False][MatchTier.EXACT.value],
        "false_near": table[False][MatchTier.NEAR.value],
        "missed_same": table[True][MatchTier.NONE.value],
    }
