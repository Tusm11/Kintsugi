"""Tests for the v2 fix-cache (src/fix_cache.py), on both backends."""

import pytest

from src.attribution_similarity import MatchTier, fingerprint
from src.fix_cache import (
    CacheLookup,
    FixCache,
    InMemoryFixCacheBackend,
    RedisFixCacheBackend,
    failure_signature,
)
from src.models import Attribution, Run


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_attr(cause="Removed return statement in compute_price()", cf="pass", source="model", against=None):
    return Attribution(
        claimed_cause=cause,
        evidence_for=["The diff changed the return statement", "Test expected 42 but got 41"],
        evidence_against=list(against or []),
        alternatives_considered=[{"cause": "flaky", "why_rejected": "deterministic"}],
        counterfactual_result=cf,
        attribution_source=source,
    )


def make_run(repo="user/repo", logs="FAILED tests/test_calc.py::test_total - AssertionError: expected 42 but got 41",
             diff="--- a/src/calc.py\n+++ b/src/calc.py\n- return 42\n+ return 41"):
    return Run(repo=repo, failing_commit="abc", failure_logs=logs, diff=diff)


@pytest.fixture(params=["memory", "redis"])
def cache(request):
    clock = FakeClock()
    if request.param == "memory":
        backend = InMemoryFixCacheBackend(clock=clock)
    else:
        fakeredis = pytest.importorskip("fakeredis")
        backend = RedisFixCacheBackend(fakeredis.FakeRedis())
    c = FixCache(backend=backend, ttl_seconds=100, hot_capacity=3, clock=clock)
    c._clock_handle = clock
    c._backend_kind = request.param
    return c


class TestFailureSignature:
    def test_none_when_logs_have_no_structure(self):
        assert failure_signature(make_run(logs="something went wrong")) is None

    def test_stable_across_numbers_in_assertion(self):
        a = make_run(logs="AssertionError: expected 42 but got 41")
        b = make_run(logs="AssertionError: expected 7 but got 8")
        assert failure_signature(a) == failure_signature(b)

    def test_changes_with_changed_files_and_repo(self):
        base = make_run()
        other_file = make_run(diff="--- a/src/other.py\n+++ b/src/other.py\n- x\n+ y")
        other_repo = make_run(repo="someone/else")
        assert failure_signature(base) != failure_signature(other_file)
        assert failure_signature(base) != failure_signature(other_repo)


class TestReadWrite:
    def test_store_then_exact_lookup(self, cache):
        run, attr = make_run(), make_attr()
        fp = cache.store(run, attr, "return a + b", {"tests_passed": 10})
        assert fp == fingerprint(attr)

        lookup = cache.lookup_by_attribution(run.repo, make_attr("Removed return statement in other_name()"))
        assert lookup.tier == MatchTier.EXACT
        assert lookup.entry.fix_patch == "return a + b"
        assert cache.get(run.repo, fp).hit_count == 1

    def test_signature_lookup(self, cache):
        run = make_run()
        cache.store(run, make_attr(), "patch")
        again = make_run()  # same failure, new Run
        lookup = cache.lookup_by_signature(again)
        assert lookup.is_exact and lookup.path == "signature"
        assert cache.metrics.counters["lookup.signature_hit"] == 1

    def test_signature_reuse_can_be_disabled(self, cache):
        cache.store(make_run(), make_attr(), "patch")
        cache.signature_reuse = False
        assert cache.lookup_by_signature(make_run()).tier == MatchTier.NONE

    def test_near_lookup_returns_entry_but_is_not_exact(self, cache):
        run = make_run()
        cache.store(run, make_attr("Removed return statement in compute_price()"), "patch")
        lookup = cache.lookup_by_attribution(run.repo, make_attr("Removed return statement in compute_price() before loop"))
        assert lookup.tier == MatchTier.NEAR
        assert lookup.is_near and not lookup.is_exact

    def test_miss(self, cache):
        run = make_run()
        cache.store(run, make_attr(), "patch")
        assert cache.lookup_by_attribution(run.repo, make_attr("Wrong timezone for UTC timestamps")).tier == MatchTier.NONE

    def test_scoped_per_repo(self, cache):
        cache.store(make_run(repo="a/one"), make_attr(), "patch")
        assert cache.lookup_by_attribution("b/two", make_attr()).tier == MatchTier.NONE

    def test_refuses_fallback_attribution_and_empty_patch(self, cache):
        run = make_run()
        assert cache.store(run, make_attr(source="fallback_heuristic"), "patch") is None
        assert cache.store(run, make_attr(), "   ") is None
        assert cache.metrics.counters["store.skipped"] == 2


class TestInvalidation:
    def test_bust_removes_entry_signature_and_hot(self, cache):
        run = make_run()
        fp = cache.store(run, make_attr(), "patch")
        cache.bust(run.repo, fp, "test")
        assert cache.get(run.repo, fp) is None
        assert cache.lookup_by_signature(make_run()).tier == MatchTier.NONE
        assert fp not in cache.backend.hot_top(run.repo, 10)

    def test_failed_reuse_busts(self, cache):
        run = make_run()
        cache.store(run, make_attr(), "patch")
        lookup = cache.lookup_by_attribution(run.repo, make_attr())
        cache.record_reuse_result(run, lookup, passed=False)
        assert cache.get(run.repo, lookup.entry.fingerprint) is None
        assert cache.metrics.counters["bust"] == 1

    def test_ttl_expiry_in_memory(self):
        clock = FakeClock()
        cache = FixCache(backend=InMemoryFixCacheBackend(clock=clock), ttl_seconds=100, clock=clock)
        run = make_run()
        fp = cache.store(run, make_attr(), "patch")
        clock.t += 101
        assert cache.get(run.repo, fp) is None
        assert cache.lookup_by_signature(make_run()).tier == MatchTier.NONE

    def test_verified_reuse_refreshes_ttl_but_lookup_does_not(self):
        clock = FakeClock()
        cache = FixCache(backend=InMemoryFixCacheBackend(clock=clock), ttl_seconds=100, clock=clock)
        run = make_run()
        fp = cache.store(run, make_attr(), "patch")
        clock.t += 60
        lookup = cache.lookup_by_attribution(run.repo, make_attr())  # hit, no refresh
        clock.t += 30
        cache.record_reuse_result(run, lookup, passed=True)          # refresh at t+90
        clock.t += 80                                                 # t+170: alive only if refreshed
        assert cache.get(run.repo, fp) is not None
        clock.t += 30                                                 # t+200 > 90+100
        assert cache.get(run.repo, fp) is None

    def test_hot_trim_spills_to_cold(self, cache):
        run = make_run()
        causes = [
            "Removed return statement in f()",
            "Wrong timezone for UTC timestamps",
            "Missing await before network call",
            "Dictionary key renamed without migration",
        ]
        fps = []
        for i, cause in enumerate(causes):
            cache._clock_handle.t += 1  # hot-set scores come from the cache clock
            fps.append(cache.store(run, make_attr(cause), f"patch-{i}"))
        hot = cache.backend.hot_top(run.repo, 10)
        assert len(hot) == 3 and fps[0] not in hot
        # Spilled, not discarded: still reachable by exact fingerprint.
        assert cache.get(run.repo, fps[0]) is not None
        lookup = cache.lookup_by_attribution(run.repo, make_attr(causes[0]))
        assert lookup.is_exact
        # ... and the hit promoted it back into the hot tier.
        assert fps[0] in cache.backend.hot_top(run.repo, 10)
