"""Fix-Cache: reuse verified semantic fixes for repeat root causes (v2).

What is cached
--------------
Only fixes that the Verifier actually passed, from model-sourced attributions.
Each entry is keyed by the attribution fingerprint (see attribution_similarity)
and holds::

    { fix_patch, attribution, verification_result, last_seen, hit_count,
      failure_signature, features, created_at }

What a hit buys you — and what it never buys you
------------------------------------------------
* EXACT hit -> the pipeline skips diagnosis and/or generation, but the cached
  patch still goes through Output Guardrail -> Confidence Gate -> Scope Guard ->
  Verifier exactly like a freshly generated one. Caching saves diagnosis +
  generation cost, never verification cost.
* NEAR hit  -> the cached *patch is never applied*. Only its Attribution is
  handed to the context-bucket builder as seed context (bucket 1).
* NONE      -> cold start.

Two lookup paths
----------------
1. ``lookup_by_signature(run)`` — *before* attribution. The fingerprint is
   derived from an Attribution, which costs ~4 model calls to produce, so a
   fingerprint lookup alone cannot "skip the Attribution Engine". A cheap,
   deterministic *failure signature* (repo + exception types + failing test ids
   + normalized assertion text + changed files, no model involved) is stored as a
   secondary index pointing at the fingerprint. A signature hit lets the
   pipeline skip attribution AND generation. Disable with
   FIX_CACHE_SIGNATURE_REUSE=false if you only want post-attribution reuse.
2. ``lookup_by_attribution(repo, attribution)`` — after attribution, the
   fingerprint/similarity lookup from the design: EXACT skips generation, NEAR
   seeds bucket 1.

Storage (Redis layout, matches the README's hot/cold design)
------------------------------------------------------------
    kintsugi:fixcache:{repo}:entry:{fingerprint}  Hash   (cold tier, TTL)
    kintsugi:fixcache:{repo}:sig:{signature}      String -> fingerprint (TTL)
    kintsugi:fixcache:{repo}:hot                  Sorted Set, score=last_seen

The hot Sorted Set is capped at ``hot_capacity``. Trimming it *spills* rather
than discards: a trimmed fingerprint is no longer scanned for near matches, but
its Hash stays in the cold tier until TTL and is still reachable by exact
fingerprint or signature — and a hit promotes it back to hot.

Invalidation
------------
* TTL (default 7 days, FIX_CACHE_TTL_SECONDS): repo state drifts, so entries
  age out. A lookup hit does NOT refresh TTL; only a *verified* reuse does.
* Explicit bust: if a reused patch fails re-verification, the entry, its
  signature pointer and its hot-set membership are deleted immediately, so a
  bad reuse cannot calcify in the cache.

Entries are scoped per repo — a patch is only ever meaningful against the repo
it was verified in, and cross-repo reuse would leak code between repos.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.attribution_similarity import (
    AttributionFeatures,
    AttributionSimilarity,
    EvidenceShape,
    MatchTier,
    extract_features,
    normalize_cause,
)
from src.cache_metrics import CacheMetrics
from src.models import Attribution, Run


DEFAULT_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_HOT_CAPACITY = 200
DEFAULT_NEAR_SCAN_LIMIT = 50
SIGNATURE_VERSION = 1


# ---------------------------------------------------------------------------
# Failure signature (pre-attribution, deterministic, no model)
# ---------------------------------------------------------------------------

_EXC_RE = re.compile(r"\b([A-Z]\w*(?:Error|Exception|Failure))\b")
_FAILED_TEST_RE = re.compile(r"^\s*(?:FAILED|FAIL|ERROR)\s+(\S+)", re.MULTILINE)
_PARAM_ID_RE = re.compile(r"\[.*?\]$")
_ASSERT_LINE_RE = re.compile(r"(assert|expected|Error:|Exception:)", re.IGNORECASE)
_DIFF_FILE_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:[ab]/)?(\S+)", re.MULTILINE)


def changed_files(diff: str) -> List[str]:
    """Files named in a unified diff's ---/+++ headers (excluding /dev/null)."""
    return sorted({f for f in _DIFF_FILE_RE.findall(diff or "") if f != "/dev/null"})


def failure_signature(run: Run) -> Optional[str]:
    """Deterministic key for "the same failure", computed without any model call.

    Returns None when the logs carry nothing structural (no exception type, no
    failing test id, no assertion line) — such runs never take the signature
    shortcut, because an empty signature would match every other empty one.
    """
    logs = run.failure_logs or ""
    exceptions = sorted(set(_EXC_RE.findall(logs)))
    tests = sorted({_PARAM_ID_RE.sub("", t) for t in _FAILED_TEST_RE.findall(logs)})
    assertion_patterns: List[str] = []
    for line in logs.splitlines():
        if _ASSERT_LINE_RE.search(line):
            tokens = normalize_cause(line)
            if tokens:
                assertion_patterns.append(" ".join(tokens))
        if len(assertion_patterns) >= 3:
            break

    if not exceptions and not tests and not assertion_patterns:
        return None

    blob = json.dumps(
        {
            "v": SIGNATURE_VERSION,
            "repo": run.repo,
            "exceptions": exceptions,
            "tests": tests,
            "assertions": assertion_patterns,
            "files": changed_files(run.diff),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Entry + lookup result
# ---------------------------------------------------------------------------

@dataclass
class FixCacheEntry:
    fingerprint: str
    repo: str
    fix_patch: str
    attribution: Dict[str, Any]
    verification_result: Dict[str, Any] = field(default_factory=dict)
    failure_signature: Optional[str] = None
    features: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    last_seen: float = 0.0
    hit_count: int = 0

    def to_record(self) -> Dict[str, str]:
        """Flat str->str mapping (a Redis Hash)."""
        return {
            "fingerprint": self.fingerprint,
            "repo": self.repo,
            "fix_patch": self.fix_patch,
            "attribution": json.dumps(self.attribution),
            "verification_result": json.dumps(self.verification_result, default=str),
            "failure_signature": self.failure_signature or "",
            "features": json.dumps(self.features),
            "created_at": repr(self.created_at),
            "last_seen": repr(self.last_seen),
            "hit_count": str(self.hit_count),
        }

    @classmethod
    def from_record(cls, record: Dict[Any, Any]) -> "FixCacheEntry":
        r = {_s(k): _s(v) for k, v in record.items()}
        return cls(
            fingerprint=r["fingerprint"],
            repo=r["repo"],
            fix_patch=r["fix_patch"],
            attribution=json.loads(r["attribution"]),
            verification_result=json.loads(r.get("verification_result") or "{}"),
            failure_signature=r.get("failure_signature") or None,
            features=json.loads(r.get("features") or "{}"),
            created_at=float(r.get("created_at") or 0),
            last_seen=float(r.get("last_seen") or 0),
            hit_count=int(r.get("hit_count") or 0),
        )

    def attribution_obj(self) -> Attribution:
        return Attribution.from_dict(dict(self.attribution))

    def feature_obj(self) -> AttributionFeatures:
        """Rebuild features; recompute if the stored copy is missing/outdated."""
        f = self.features
        try:
            return AttributionFeatures(
                cause_tokens=tuple(f["cause_tokens"]),
                evidence_for=EvidenceShape(f["evidence_for"]["count"], tuple(sorted(f["evidence_for"]["categories"].items()))),
                evidence_against=EvidenceShape(f["evidence_against"]["count"], tuple(sorted(f["evidence_against"]["categories"].items()))),
                counterfactual=f["counterfactual"],
                attribution_source=f.get("attribution_source", "model"),
            )
        except (KeyError, TypeError, AttributeError):
            return extract_features(self.attribution_obj())


def _s(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)


@dataclass
class CacheLookup:
    tier: MatchTier
    entry: Optional[FixCacheEntry] = None
    score: float = 0.0
    breakdown: Dict[str, float] = field(default_factory=dict)
    path: str = "attribution"  # "signature" | "attribution"
    reason: str = ""

    @property
    def is_exact(self) -> bool:
        return self.tier == MatchTier.EXACT and self.entry is not None

    @property
    def is_near(self) -> bool:
        return self.tier == MatchTier.NEAR and self.entry is not None


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class InMemoryFixCacheBackend:
    """Process-local backend with the same semantics as the Redis one.

    Default when REDIS_URL is not set, and what the tests use. Not shared across
    worker processes — use RedisFixCacheBackend for that.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._entries: Dict[Tuple[str, str], Tuple[Dict[str, str], float]] = {}
        self._sigs: Dict[Tuple[str, str], Tuple[str, float]] = {}
        self._hot: Dict[str, Dict[str, float]] = {}

    def _alive(self, expires_at: float) -> bool:
        return expires_at > self.clock()

    def get_entry(self, repo: str, fp: str) -> Optional[Dict[str, str]]:
        item = self._entries.get((repo, fp))
        if item is None:
            return None
        record, expires = item
        if not self._alive(expires):
            del self._entries[(repo, fp)]
            return None
        return dict(record)

    def put_entry(self, repo: str, fp: str, record: Dict[str, str], ttl: int) -> None:
        self._entries[(repo, fp)] = (dict(record), self.clock() + ttl)

    def update_fields(self, repo: str, fp: str, fields: Dict[str, str]) -> None:
        item = self._entries.get((repo, fp))
        if item is not None:
            item[0].update(fields)

    def incr_hit(self, repo: str, fp: str) -> None:
        item = self._entries.get((repo, fp))
        if item is not None:
            item[0]["hit_count"] = str(int(item[0].get("hit_count", "0")) + 1)

    def refresh_ttl(self, repo: str, fp: str, ttl: int) -> None:
        item = self._entries.get((repo, fp))
        if item is not None:
            self._entries[(repo, fp)] = (item[0], self.clock() + ttl)

    def delete_entry(self, repo: str, fp: str) -> None:
        self._entries.pop((repo, fp), None)

    def set_signature(self, repo: str, sig: str, fp: str, ttl: int) -> None:
        self._sigs[(repo, sig)] = (fp, self.clock() + ttl)

    def get_signature(self, repo: str, sig: str) -> Optional[str]:
        item = self._sigs.get((repo, sig))
        if item is None:
            return None
        if not self._alive(item[1]):
            del self._sigs[(repo, sig)]
            return None
        return item[0]

    def delete_signature(self, repo: str, sig: str) -> None:
        self._sigs.pop((repo, sig), None)

    def hot_add(self, repo: str, fp: str, score: float) -> None:
        self._hot.setdefault(repo, {})[fp] = score

    def hot_top(self, repo: str, n: int) -> List[str]:
        members = self._hot.get(repo, {})
        return [fp for fp, _ in sorted(members.items(), key=lambda kv: kv[1], reverse=True)[:n]]

    def hot_remove(self, repo: str, fp: str) -> None:
        self._hot.get(repo, {}).pop(fp, None)

    def hot_trim(self, repo: str, capacity: int) -> None:
        members = self._hot.get(repo, {})
        if len(members) > capacity:
            keep = set(self.hot_top(repo, capacity))
            for fp in list(members):
                if fp not in keep:
                    del members[fp]


class RedisFixCacheBackend:
    """Redis backend: Hash per entry (cold, TTL) + Sorted Set hot index."""

    def __init__(self, client: Any, prefix: str = "kintsugi:fixcache"):
        self.r = client
        self.prefix = prefix

    @classmethod
    def from_url(cls, url: str, prefix: str = "kintsugi:fixcache") -> "RedisFixCacheBackend":
        import redis  # imported lazily so redis is only needed when used

        return cls(redis.Redis.from_url(url), prefix=prefix)

    def _k(self, repo: str, *parts: str) -> str:
        return ":".join((self.prefix, repo) + parts)

    def get_entry(self, repo: str, fp: str) -> Optional[Dict[str, str]]:
        data = self.r.hgetall(self._k(repo, "entry", fp))
        return {_s(k): _s(v) for k, v in data.items()} if data else None

    def put_entry(self, repo: str, fp: str, record: Dict[str, str], ttl: int) -> None:
        key = self._k(repo, "entry", fp)
        pipe = self.r.pipeline()
        pipe.delete(key)
        pipe.hset(key, mapping=record)
        pipe.expire(key, ttl)
        pipe.execute()

    def update_fields(self, repo: str, fp: str, fields: Dict[str, str]) -> None:
        key = self._k(repo, "entry", fp)
        if self.r.exists(key):
            self.r.hset(key, mapping=fields)

    def incr_hit(self, repo: str, fp: str) -> None:
        key = self._k(repo, "entry", fp)
        if self.r.exists(key):
            self.r.hincrby(key, "hit_count", 1)

    def refresh_ttl(self, repo: str, fp: str, ttl: int) -> None:
        self.r.expire(self._k(repo, "entry", fp), ttl)

    def delete_entry(self, repo: str, fp: str) -> None:
        self.r.delete(self._k(repo, "entry", fp))

    def set_signature(self, repo: str, sig: str, fp: str, ttl: int) -> None:
        self.r.set(self._k(repo, "sig", sig), fp, ex=ttl)

    def get_signature(self, repo: str, sig: str) -> Optional[str]:
        value = self.r.get(self._k(repo, "sig", sig))
        return _s(value) if value is not None else None

    def delete_signature(self, repo: str, sig: str) -> None:
        self.r.delete(self._k(repo, "sig", sig))

    def hot_add(self, repo: str, fp: str, score: float) -> None:
        self.r.zadd(self._k(repo, "hot"), {fp: score})

    def hot_top(self, repo: str, n: int) -> List[str]:
        return [_s(m) for m in self.r.zrevrange(self._k(repo, "hot"), 0, max(n - 1, 0))]

    def hot_remove(self, repo: str, fp: str) -> None:
        self.r.zrem(self._k(repo, "hot"), fp)

    def hot_trim(self, repo: str, capacity: int) -> None:
        # Drop the lowest-scored (least recently seen) members beyond capacity.
        # Their Hashes are untouched: spill to cold, not discard.
        self.r.zremrangebyrank(self._k(repo, "hot"), 0, -(capacity + 1))


# ---------------------------------------------------------------------------
# FixCache
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class FixCache:
    """Read/write path for verified fixes. See module docstring for semantics."""

    def __init__(
        self,
        backend: Optional[Any] = None,
        similarity: Optional[AttributionSimilarity] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        hot_capacity: int = DEFAULT_HOT_CAPACITY,
        near_scan_limit: int = DEFAULT_NEAR_SCAN_LIMIT,
        signature_reuse: bool = True,
        metrics: Optional[CacheMetrics] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.clock = clock
        self.backend = backend if backend is not None else InMemoryFixCacheBackend(clock=clock)
        self.similarity = similarity or AttributionSimilarity()
        self.ttl_seconds = int(ttl_seconds)
        self.hot_capacity = int(hot_capacity)
        self.near_scan_limit = int(near_scan_limit)
        self.signature_reuse = signature_reuse
        self.metrics = metrics or CacheMetrics()

    @classmethod
    def from_env(cls, metrics: Optional[CacheMetrics] = None) -> Optional["FixCache"]:
        """Build from environment; returns None when FIX_CACHE_ENABLED=false.

        FIX_CACHE_ENABLED        default true
        REDIS_URL                if set, use Redis; otherwise in-memory
        FIX_CACHE_TTL_SECONDS    default 604800 (7 days)
        FIX_CACHE_HOT_CAPACITY   default 200
        FIX_CACHE_NEAR_SCAN_LIMIT default 50
        FIX_CACHE_NEAR_THRESHOLD default 0.65
        FIX_CACHE_SIGNATURE_REUSE default true
        FIX_CACHE_METRICS_PATH   optional JSONL file for metrics events
        """
        if not _env_bool("FIX_CACHE_ENABLED", True):
            return None
        redis_url = os.getenv("REDIS_URL")
        backend = RedisFixCacheBackend.from_url(redis_url) if redis_url else InMemoryFixCacheBackend()
        near = os.getenv("FIX_CACHE_NEAR_THRESHOLD")
        similarity = AttributionSimilarity(near_threshold=float(near)) if near else AttributionSimilarity()
        return cls(
            backend=backend,
            similarity=similarity,
            ttl_seconds=int(os.getenv("FIX_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS)),
            hot_capacity=int(os.getenv("FIX_CACHE_HOT_CAPACITY", DEFAULT_HOT_CAPACITY)),
            near_scan_limit=int(os.getenv("FIX_CACHE_NEAR_SCAN_LIMIT", DEFAULT_NEAR_SCAN_LIMIT)),
            signature_reuse=_env_bool("FIX_CACHE_SIGNATURE_REUSE", True),
            metrics=metrics,
        )

    # -- internal ----------------------------------------------------------

    def _load(self, repo: str, fp: str) -> Optional[FixCacheEntry]:
        record = self.backend.get_entry(repo, fp)
        if record is None:
            return None
        try:
            return FixCacheEntry.from_record(record)
        except (KeyError, ValueError, json.JSONDecodeError):
            # Corrupt entry: remove it rather than let it poison lookups.
            self.backend.delete_entry(repo, fp)
            self.backend.hot_remove(repo, fp)
            return None

    def _touch(self, entry: FixCacheEntry) -> None:
        """Record a lookup hit: hit_count++, last_seen=now, promote to hot.

        Does not refresh TTL — only a verified reuse does that.
        """
        now = self.clock()
        self.backend.incr_hit(entry.repo, entry.fingerprint)
        self.backend.update_fields(entry.repo, entry.fingerprint, {"last_seen": repr(now)})
        self.backend.hot_add(entry.repo, entry.fingerprint, now)
        self.backend.hot_trim(entry.repo, self.hot_capacity)
        entry.hit_count += 1
        entry.last_seen = now

    # -- read path ---------------------------------------------------------

    def lookup_by_signature(self, run: Run) -> CacheLookup:
        """Pre-attribution lookup. Only ever returns EXACT or NONE."""
        if not self.signature_reuse:
            return CacheLookup(MatchTier.NONE, path="signature", reason="signature reuse disabled")
        sig = failure_signature(run)
        if sig is None:
            self.metrics.record("lookup.signature_miss", run_id=run.id, repo=run.repo, reason="no_signature")
            return CacheLookup(MatchTier.NONE, path="signature", reason="logs carry no structural signature")
        fp = self.backend.get_signature(run.repo, sig)
        entry = self._load(run.repo, fp) if fp else None
        if entry is None:
            if fp:  # pointer outlived its entry
                self.backend.delete_signature(run.repo, sig)
            self.metrics.record("lookup.signature_miss", run_id=run.id, repo=run.repo, signature=sig)
            return CacheLookup(MatchTier.NONE, path="signature", reason="no entry for signature")
        self._touch(entry)
        self.metrics.record("lookup.signature_hit", run_id=run.id, repo=run.repo, signature=sig, fingerprint=entry.fingerprint)
        return CacheLookup(MatchTier.EXACT, entry, 1.0, {}, "signature", "failure signature matched a verified fix")

    def lookup_by_attribution(self, repo: str, attribution: Attribution, run_id: Optional[str] = None) -> CacheLookup:
        """Post-attribution lookup: EXACT by fingerprint, else best NEAR in the hot tier."""
        features = extract_features(attribution)

        candidates: List[Tuple[FixCacheEntry, AttributionFeatures]] = []
        direct = self._load(repo, features.fingerprint)
        if direct is not None:
            candidates.append((direct, direct.feature_obj()))

        for fp in self.backend.hot_top(repo, self.near_scan_limit):
            if direct is not None and fp == direct.fingerprint:
                continue
            entry = self._load(repo, fp)
            if entry is None:
                self.backend.hot_remove(repo, fp)  # expired in cold tier: lazy cleanup
                continue
            candidates.append((entry, entry.feature_obj()))

        entry, result = self.similarity.best_match(features, candidates)
        if entry is not None:
            self._touch(entry)
        self.metrics.record(
            f"lookup.{result.tier.value}",
            run_id=run_id,
            repo=repo,
            fingerprint=features.fingerprint,
            matched=entry.fingerprint if entry else None,
            score=result.score,
            candidates=len(candidates),
        )
        return CacheLookup(result.tier, entry, result.score, result.breakdown, "attribution", result.reason)

    # -- write path --------------------------------------------------------

    def store(
        self,
        run: Run,
        attribution: Attribution,
        fix_patch: str,
        verification_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Cache a fix. Call ONLY after the Verifier passed it.

        Refuses (returns None) for fallback_heuristic attributions or an empty
        patch. Returns the fingerprint on success.
        """
        if attribution.attribution_source != "model":
            self.metrics.record("store.skipped", run_id=run.id, repo=run.repo, reason="non_model_attribution")
            return None
        if not (fix_patch or "").strip():
            self.metrics.record("store.skipped", run_id=run.id, repo=run.repo, reason="empty_patch")
            return None

        features = extract_features(attribution)
        fp = features.fingerprint
        now = self.clock()
        existing = self._load(run.repo, fp)
        sig = failure_signature(run)
        entry = FixCacheEntry(
            fingerprint=fp,
            repo=run.repo,
            fix_patch=fix_patch,
            attribution=attribution.to_dict(),
            verification_result=dict(verification_result or {}),
            failure_signature=sig,
            features=features.to_dict(),
            created_at=existing.created_at if existing else now,
            last_seen=now,
            hit_count=existing.hit_count if existing else 0,
        )
        if existing and existing.failure_signature and existing.failure_signature != sig:
            self.backend.delete_signature(run.repo, existing.failure_signature)
        self.backend.put_entry(run.repo, fp, entry.to_record(), self.ttl_seconds)
        if sig:
            self.backend.set_signature(run.repo, sig, fp, self.ttl_seconds)
        self.backend.hot_add(run.repo, fp, now)
        self.backend.hot_trim(run.repo, self.hot_capacity)
        self.metrics.record("store", run_id=run.id, repo=run.repo, fingerprint=fp, overwrote=existing is not None)
        return fp

    def record_reuse_result(self, run: Run, lookup: CacheLookup, passed: bool) -> None:
        """Feed the Verifier's verdict on a reused patch back into the cache."""
        entry = lookup.entry
        if entry is None:
            return
        if passed:
            self.backend.refresh_ttl(entry.repo, entry.fingerprint, self.ttl_seconds)
            if entry.failure_signature:
                self.backend.set_signature(entry.repo, entry.failure_signature, entry.fingerprint, self.ttl_seconds)
            self.metrics.record("reuse.verified_pass", run_id=run.id, repo=run.repo, fingerprint=entry.fingerprint, path=lookup.path)
        else:
            self.metrics.record("reuse.verified_fail", run_id=run.id, repo=run.repo, fingerprint=entry.fingerprint, path=lookup.path)
            self.bust(entry.repo, entry.fingerprint, "verification_failed", run_id=run.id)

    def bust(self, repo: str, fp: str, reason: str, run_id: Optional[str] = None) -> None:
        """Remove an entry, its signature pointer and its hot-set membership."""
        entry = self._load(repo, fp)
        if entry is not None and entry.failure_signature:
            self.backend.delete_signature(repo, entry.failure_signature)
        self.backend.delete_entry(repo, fp)
        self.backend.hot_remove(repo, fp)
        self.metrics.record("bust", run_id=run_id, repo=repo, fingerprint=fp, reason=reason)

    def get(self, repo: str, fp: str) -> Optional[FixCacheEntry]:
        """Read an entry without counting it as a hit (inspection/tests)."""
        return self._load(repo, fp)
