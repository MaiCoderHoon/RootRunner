"""Caching layer for the iterative DNS resolver (Caching & TTL component).

CachingResolver wraps Ayush's IterativeResolver and has the same
resolve(domain, qtype) method, so anything that used the plain resolver
(experiments, a future DNS server) can use this one instead.

What it does:
  * remembers answers until their TTL runs out (positive caching)
  * remembers "doesn't exist" / "no records of that type" answers
    (negative caching, RFC 2308)
  * never caches errors (a failed lookup is not the same as "doesn't exist")
  * on a hit, reports the REMAINING ttl, not the original one
  * throws out the least recently used entry when the cache is full (LRU)
  * keeps counters (stats()) and one log line per lookup (CSV export)
"""

import csv
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, asdict, fields, replace

from src.dns_resolver.parser import RecordType
from src.dns_resolver.resolver import IterativeResolver, normalize_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL rules
# ---------------------------------------------------------------------------

def positive_ttl(result):
    """Positive answer: cache only as long as the SHORTEST-lived record.

    result.answers can hold CNAMEs plus the final records. If the CNAME says
    3600 but the A record says 60, the answer is only good for 60 seconds.
    """
    return min(r.ttl for r in result.answers)


def negative_ttl(result):
    """Negative answer (NXDOMAIN or NODATA): RFC 2308.

    TTL = min(SOA record's own TTL, SOA MINIMUM field).
    The SOA comes back as raw hex. The last 20 bytes of an SOA are five
    32-bit numbers and MINIMUM is the last one, so we read the LAST 8 hex
    characters. (We can't read from the front: the first two fields are
    domain names that may use compression pointers.)
    """
    soa = next((r for r in result.authority if r.type == RecordType.SOA), None)
    if soa is None:
        return 0  # no SOA -> RFC 2308 says don't cache
    minimum = int(soa.data[-8:], 16)
    return min(soa.ttl, minimum)


def cache_ttl(result):
    """How many seconds this result may be cached (0 = don't cache)."""
    if result.answers:
        return positive_ttl(result)
    return negative_ttl(result)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    result: object      # the ResolutionResult exactly as the resolver gave it
    stored_at: float    # clock() when we stored it
    expires_at: float   # clock() value at which it stops being valid
    negative: bool      # True for NXDOMAIN / NODATA


@dataclass
class LookupLogEntry:
    """One line per lookup. This is what Aryaman's experiments will read."""
    timestamp: float       # wall-clock time of the lookup (time.time())
    name: str
    qtype: str
    outcome: str           # HIT / NEGATIVE_HIT / MISS / ERROR
    latency_ms: float
    queries_sent: int      # 0 on a hit, len(result.trace) on a miss
    hops: int
    rcode: str
    ttl: int               # ttl cached (miss) or ttl remaining (hit)


LOG_FIELDS = [f.name for f in fields(LookupLogEntry)]


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class CachingResolver:
    def __init__(self, resolver=None, max_entries=1000, clock=time.monotonic):
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.resolver = resolver if resolver is not None else IterativeResolver()
        self.max_entries = max_entries
        # clock is injectable so tests can fake time instead of sleeping.
        # monotonic = never jumps backwards when the system clock changes.
        self.clock = clock

        self._cache = OrderedDict()  # (name, qtype) -> CacheEntry, oldest first
        self.hits = 0
        self.negative_hits = 0
        self.misses = 0
        self.errors = 0
        self.evictions = 0
        self.log = []                # list of LookupLogEntry

    # -- main entry point ---------------------------------------------------

    def resolve(self, domain, record_type=RecordType.A):
        # Same signature as IterativeResolver.resolve (record_type defaults to A),
        # so this class can be dropped in anywhere the plain resolver is used.
        # Same normalizer as the resolver, so "GitHub.com." == "github.com"
        key = (normalize_name(domain), RecordType(record_type))
        started = time.perf_counter()
        now = self.clock()

        entry = self._cache.get(key)
        if entry is not None and entry.expires_at > now:
            # ---- HIT ----
            self._cache.move_to_end(key)  # mark as recently used
            result = self._aged_copy(entry, now)
            if entry.negative:
                self.negative_hits += 1
                outcome = "NEGATIVE_HIT"
            else:
                self.hits += 1
                outcome = "HIT"
            ttl_left = math.ceil(entry.expires_at - now)
            self._log(key, outcome, started, 0, result, ttl_left)
            return result

        if entry is not None:
            del self._cache[key]  # expired, throw it away

        # ---- MISS ----
        self.misses += 1
        try:
            result = self.resolver.resolve(domain, record_type)
        except Exception:
            # ResolutionError, HopLimitExceeded, ValueError ... never cached.
            # "We couldn't find out" is not "it doesn't exist".
            self.errors += 1
            self._log(key, "ERROR", started, 0, None, 0)
            raise

        ttl = cache_ttl(result)
        if ttl > 0:  # TTL 0 means "do not cache"
            stored_at = self.clock()
            self._store(key, CacheEntry(
                result=result,
                stored_at=stored_at,
                expires_at=stored_at + ttl,
                negative=not result.answers,
            ))
        self._log(key, "MISS", started, len(result.trace), result, ttl)
        return result

    # -- helpers ------------------------------------------------------------

    def _store(self, key, entry):
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)  # drop least recently used
            self.evictions += 1

    def _aged_copy(self, entry, now):
        """Copy of the cached result with TTLs counted down to 'now'.

        We return a copy so the stored entry is never changed. hops and trace
        are emptied because a hit sends zero queries.
        """
        elapsed = now - entry.stored_at
        remaining = math.ceil(entry.expires_at - now)  # always >= 1 here

        def aged(records):
            return [
                replace(r, ttl=max(0, min(math.ceil(r.ttl - elapsed), remaining)))
                for r in records
            ]

        return replace(
            entry.result,
            answers=aged(entry.result.answers),
            authority=aged(entry.result.authority),
            hops=0,
            trace=[],
        )

    def _log(self, key, outcome, started, queries, result, ttl):
        latency_ms = (time.perf_counter() - started) * 1000
        rcode = getattr(getattr(result, "rcode", None), "name", "") if result else ""
        row = LookupLogEntry(
            timestamp=time.time(),
            name=key[0],
            qtype=key[1].name,
            outcome=outcome,
            latency_ms=round(latency_ms, 3),
            queries_sent=queries,
            hops=result.hops if result else 0,
            rcode=rcode,
            ttl=ttl,
        )
        self.log.append(row)
        logger.info("%s %s %s %.2fms queries=%d",
                    outcome, key[0], key[1].name, latency_ms, queries)

    # -- stats, logging, housekeeping ----------------------------------------

    def stats(self):
        self._purge_expired()
        lookups = self.hits + self.negative_hits + self.misses
        served_from_cache = self.hits + self.negative_hits
        return {
            "hits": self.hits,
            "negative_hits": self.negative_hits,
            "misses": self.misses,
            "errors": self.errors,
            "evictions": self.evictions,
            "lookups": lookups,
            "size": len(self._cache),
            "hit_ratio": round(served_from_cache / lookups, 4) if lookups else 0.0,
        }

    def export_log_csv(self, path):
        """Write the per-lookup log to a CSV file. Returns the number of rows."""
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
            for row in self.log:
                writer.writerow(asdict(row))
        return len(self.log)

    def clear(self):
        """Empty the cache (counters and log are kept)."""
        self._cache.clear()

    def flush(self, domain, qtype=None):
        """Remove one name from the cache (one qtype, or all if qtype is None)."""
        name = normalize_name(domain)
        for key in [k for k in self._cache if k[0] == name
                    and (qtype is None or k[1] == RecordType(qtype))]:
            del self._cache[key]

    def _purge_expired(self):
        now = self.clock()
        for key in [k for k, e in self._cache.items() if e.expires_at <= now]:
            del self._cache[key]

    def __len__(self):
        return len(self._cache)
