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

Delegation cache (optional, on by default when no resolver is given):
  DelegatingResolver is Ayush's IterativeResolver plus a DelegationCache that
  remembers "which nameservers run zone X" (the NS + glue from referrals).
  A lookup for a NEW name can then start at e.g. the .com servers (or even
  example.com's own servers) and skip the root / TLD hops.
      CachingResolver(IterativeResolver())  -> answer cache only
      CachingResolver()                     -> answer cache + delegation cache
"""

import csv
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, asdict, fields, replace

from src.dns_resolver.parser import RecordType
from src.dns_resolver.resolver import (
    HopLimitExceeded,
    IterativeResolver,
    ResolutionError,
    _NameServer,
    _Context,
    _validate_name,
    normalize_name,
)

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
    start_zone: str        # zone of the first query: '.' = root, 'com', ... ; '' on a hit
    hops: int
    rcode: str
    ttl: int               # ttl cached (miss) or ttl remaining (hit)


LOG_FIELDS = [f.name for f in fields(LookupLogEntry)]


# ---------------------------------------------------------------------------
# Delegation cache: remember NS + glue so new names can skip the root / TLD hops
# ---------------------------------------------------------------------------

class DelegationCache:
    """zone -> nameservers, each entry valid for the shortest TTL of its records.

    nameservers is a list of (ns_name, (ip, ...)). An NS with no IP is a
    "glueless" one; the resolver looks its address up again when needed.
    When full, the oldest stored entry is dropped.
    """

    def __init__(self, max_entries=500, clock=time.monotonic):
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self.clock = clock
        self._entries = OrderedDict()   # zone -> (expires_at, nameservers)
        self.evictions = 0

    def store(self, zone, nameservers, ttl):
        if ttl <= 0 or not zone:        # TTL 0 = don't cache; root hints are fixed
            return
        servers = [(name, tuple(ips)) for name, ips in nameservers]
        self._entries[zone] = (self.clock() + ttl, servers)
        self._entries.move_to_end(zone)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1

    def live(self):
        """All delegations that haven't expired: {zone: [(ns_name, (ips...)), ...]}"""
        now = self.clock()
        for zone in [z for z, (exp, _) in self._entries.items() if exp <= now]:
            del self._entries[zone]
        return {zone: servers for zone, (_, servers) in self._entries.items()}

    def invalidate(self, zone):
        self._entries.pop(zone, None)

    def clear(self):
        self._entries.clear()

    def __len__(self):
        return len(self.live())


class DelegatingResolver(IterativeResolver):
    """Ayush's resolver + a delegation cache, without editing his file.

    How it works: IterativeResolver keeps the delegations it learns during one
    lookup in ctx.delegations (zone -> nameservers). We
      1. start each lookup with the still-valid cached delegations put in there,
         so it begins at the closest known zone instead of the root, and
      2. grab every new referral it follows (with TTL = min of the NS and glue
         TTLs) and save it in the DelegationCache.
    If a lookup that started from cached delegations fails, they may be stale:
    we throw away the ones that were used and redo the lookup from the root.

    Note: this uses Ayush's private _Context and _NameServer classes and his
    _extract_referral method, so tell him if he ever renames them.
    """

    def __init__(self, *args, delegation_cache=None, clock=time.monotonic, **kwargs):
        super().__init__(*args, **kwargs)
        self.delegation_cache = (delegation_cache if delegation_cache is not None
                                 else DelegationCache(clock=clock))
        self._learned = {}   # zone -> (nameservers, ttl) seen during the current walk

    def resolve(self, domain, record_type=RecordType.A):
        qname = normalize_name(domain)
        _validate_name(qname)
        record_type = RecordType(record_type)

        seeded = self.delegation_cache.live()
        try:
            return self._walk(qname, record_type, seeded)
        except HopLimitExceeded:
            raise
        except ResolutionError as first_error:
            if not seeded:
                raise
            # Cached delegations may be stale: forget the ones this walk used, retry cold.
            used = {step.zone for step in first_error.trace}
            for zone in seeded:
                if zone in used:
                    self.delegation_cache.invalidate(zone)
            try:
                result = self._walk(qname, record_type, {})
            except ResolutionError as second_error:
                second_error.trace = first_error.trace + second_error.trace
                raise
            result.trace = first_error.trace + result.trace   # count the wasted queries too
            return result

    def _walk(self, qname, record_type, seeded):
        ctx = _Context(self.max_hops, self.root_servers)
        for zone, nameservers in seeded.items():
            ctx.delegations[zone] = [_NameServer(name, list(ips)) for name, ips in nameservers]
        self._learned = {}
        try:
            result = self._resolve(ctx, qname, record_type)
        except ResolutionError as e:
            e.trace = ctx.trace
            raise
        result.trace = ctx.trace
        for zone, (nameservers, ttl) in self._learned.items():
            self.delegation_cache.store(zone, nameservers, ttl)
        return result

    def _extract_referral(self, resp, qname, zone):
        referral = super()._extract_referral(resp, qname, zone)
        if referral is not None:
            child_zone, nameservers = referral
            # copy NOW: the resolver later fills in IPs of glueless servers in place
            copy = [(ns.name, tuple(ns.ips)) for ns in nameservers]
            self._learned[child_zone] = (copy, self._delegation_ttl(resp, child_zone, nameservers))
        return referral

    @staticmethod
    def _delegation_ttl(resp, child_zone, nameservers):
        """Shortest TTL among the NS records and the glue A records we kept."""
        ttls = [r.ttl for r in resp.authority
                if r.type == RecordType.NS and normalize_name(r.name) == child_zone]
        glue_names = {ns.name for ns in nameservers if ns.ips}
        ttls += [r.ttl for r in resp.additional
                 if r.type == RecordType.A and normalize_name(r.name) in glue_names]
        return min(ttls) if ttls else 0


def _start_zone(trace):
    """Zone of the first query of the final attempt ('.' = root, '' = no queries)."""
    firsts = [s for s in trace
              if getattr(s, "depth", None) == 0 and getattr(s, "hop", None) == 1]
    return (firsts[-1].zone or ".") if firsts else ""


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

class CachingResolver:
    def __init__(self, resolver=None, max_entries=1000, clock=time.monotonic):
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        # no resolver given -> answer cache AND delegation cache. Pass a plain
        # IterativeResolver() to get the answer cache only.
        self.resolver = (resolver if resolver is not None
                         else DelegatingResolver(clock=clock))
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
        self.delegation_hits = 0     # misses that started below the root (delegation cache helped)
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
            self._log(key, outcome, started, 0, result, ttl_left, "")
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
            self._log(key, "ERROR", started, 0, None, 0, "")
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
        start_zone = _start_zone(result.trace)
        if start_zone not in ("", "."):
            self.delegation_hits += 1
        self._log(key, "MISS", started, len(result.trace), result, ttl, start_zone)
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

    def _log(self, key, outcome, started, queries, result, ttl, start_zone):
        latency_ms = (time.perf_counter() - started) * 1000
        rcode = getattr(getattr(result, "rcode", None), "name", "") if result else ""
        row = LookupLogEntry(
            timestamp=time.time(),
            name=key[0],
            qtype=key[1].name,
            outcome=outcome,
            latency_ms=round(latency_ms, 3),
            queries_sent=queries,
            start_zone=start_zone,
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
        delegations = getattr(self.resolver, "delegation_cache", None)
        lookups = self.hits + self.negative_hits + self.misses
        served_from_cache = self.hits + self.negative_hits
        return {
            "hits": self.hits,
            "negative_hits": self.negative_hits,
            "misses": self.misses,
            "errors": self.errors,
            "evictions": self.evictions,
            "delegation_hits": self.delegation_hits,
            "delegation_entries": len(delegations) if delegations is not None else 0,
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

    def clear(self, include_delegations=True):
        """Empty the cache (counters and log are kept). For a true cold run this
        also empties the delegation cache unless include_delegations=False."""
        self._cache.clear()
        delegations = getattr(self.resolver, "delegation_cache", None)
        if include_delegations and delegations is not None:
            delegations.clear()

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
