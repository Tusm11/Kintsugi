"""Cache Metrics: hit/miss/bust counters and an optional JSONL event sink (v2).

The fix-cache and context-bucket tiering are *bets* — they only pay off if real
repeat failures exist and the similarity function sorts them correctly. These
metrics are how that bet gets checked (e.g. the planned tablib test pass):

  * lookup outcomes per path (signature / exact / near / none)
  * reuse outcomes (a cached patch that passed or failed re-verification)
  * busts (with reason) — a rising bust rate means the similarity tiers are
    too loose or the TTL is too long
  * which context bucket each semantic attempt used, and its token cost

Counters live in memory. If `sink_path` is set (or FIX_CACHE_METRICS_PATH in
the environment), every event is also appended as one JSON line so runs can be
analysed offline without Redis or a database.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kintsugi.fix_cache")


class CacheMetrics:
    """Thread-safe counters + append-only event list for cache/bucket behaviour."""

    def __init__(self, sink_path: Optional[str] = None, keep_events: int = 1000):
        self.sink_path = sink_path if sink_path is not None else os.getenv("FIX_CACHE_METRICS_PATH") or None
        self.keep_events = keep_events
        self.counters: Counter = Counter()
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> Dict[str, Any]:
        """Count `event` and keep a structured record of it.

        Never raises: a metrics failure must not break a repair Run.
        """
        record = {"ts": datetime.utcnow().isoformat(), "event": event, **fields}
        with self._lock:
            self.counters[event] += 1
            bucket = fields.get("bucket_tier")
            if bucket is not None:
                self.counters[f"{event}.bucket_{bucket}"] += 1
            tokens = fields.get("tokens_used")
            if isinstance(tokens, (int, float)):
                self.counters[f"{event}.tokens"] += int(tokens)
            self.events.append(record)
            if len(self.events) > self.keep_events:
                del self.events[: len(self.events) - self.keep_events]
        logger.info("fix_cache event %s %s", event, fields)
        if self.sink_path:
            try:
                with open(self.sink_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
            except OSError as exc:  # pragma: no cover - disk problems only
                logger.warning("could not write cache metrics to %s: %s", self.sink_path, exc)
        return record

    def snapshot(self) -> Dict[str, Any]:
        """Counters plus derived rates, safe to serialize."""
        with self._lock:
            c = dict(self.counters)
        lookups = c.get("lookup.exact", 0) + c.get("lookup.near", 0) + c.get("lookup.none", 0)
        sig_lookups = c.get("lookup.signature_hit", 0) + c.get("lookup.signature_miss", 0)
        reuses = c.get("reuse.verified_pass", 0) + c.get("reuse.verified_fail", 0)
        return {
            "counters": c,
            "exact_hit_rate": c.get("lookup.exact", 0) / lookups if lookups else None,
            "near_hit_rate": c.get("lookup.near", 0) / lookups if lookups else None,
            "signature_hit_rate": c.get("lookup.signature_hit", 0) / sig_lookups if sig_lookups else None,
            "reuse_pass_rate": c.get("reuse.verified_pass", 0) / reuses if reuses else None,
            "busts": c.get("bust", 0),
        }

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.events.clear()
