"""Tests for src/dns_resolver/cache.py

No network and no sleep(): the resolver is a stub and time is a fake clock.
Run with:  python -m pytest tests/test_cache.py -v
"""

import csv

import pytest

from src.dns_resolver import parser as parser_mod
from src.dns_resolver import resolver as resolver_mod
from src.dns_resolver.cache import (
    CachingResolver,
    cache_ttl,
    negative_ttl,
    positive_ttl,
)
from src.dns_resolver.parser import RecordType, ResponseCode


def _find(name):
    """Find a class in either module (so a different home won't break the test)."""
    for mod in (resolver_mod, parser_mod):
        if hasattr(mod, name):
            return getattr(mod, name)
    raise ImportError(f"{name} not found in parser.py or resolver.py")


ResourceRecord = _find("ResourceRecord")
ResolutionResult = _find("ResolutionResult")
ResolutionError = _find("ResolutionError")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class StubResolver:
    """Returns queued results (or raises queued exceptions) and counts calls."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def resolve(self, domain, record_type=RecordType.A):
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def rr(name, rtype, ttl, data):
    return ResourceRecord(name=name, type=rtype, ttl=ttl, data=data)


def soa_hex(minimum):
    # Junk "names" up front, then the 5 fixed 32-bit fields. MINIMUM is last.
    return "0161" * 5 + f"{1:08x}{2:08x}{3:08x}{4:08x}{minimum:08x}"


def make_result(qname, qtype, answers=(), authority=(),
                rcode=ResponseCode.NOERROR, queries=3):
    return ResolutionResult(
        qname=qname,
        qtype=qtype,
        rcode=rcode,
        answers=list(answers),
        authority=list(authority),
        cname_chain=[qname],
        hops=queries,
        trace=[object()] * queries,
    )


def a_result(name="example.com", ttl=60, ip="1.2.3.4", queries=3):
    return make_result(name, RecordType.A,
                       answers=[rr(name, RecordType.A, ttl, ip)], queries=queries)


def nxdomain_result(name="nope.com", soa_ttl=900, minimum=900):
    return make_result(name, RecordType.A, rcode=ResponseCode.NXDOMAIN,
                       authority=[rr("com", RecordType.SOA, soa_ttl, soa_hex(minimum))],
                       queries=2)


def make_cache(*outcomes, clock=None, **kwargs):
    stub = StubResolver(*outcomes)
    clock = clock or FakeClock()
    return CachingResolver(resolver=stub, clock=clock, **kwargs), stub, clock


# ---------------------------------------------------------------------------
# Basic hit / miss
# ---------------------------------------------------------------------------

def test_second_lookup_is_a_hit_and_skips_the_resolver():
    cache, stub, _ = make_cache(a_result())
    cache.resolve("example.com", RecordType.A)
    cache.resolve("example.com", RecordType.A)
    assert stub.calls == 1
    assert cache.hits == 1 and cache.misses == 1


def test_record_type_defaults_to_A_and_keyword_works():
    # same call style as IterativeResolver.resolve(domain, record_type=A)
    cache, stub, _ = make_cache(a_result())
    cache.resolve("example.com")
    cache.resolve("example.com", record_type=RecordType.A)
    assert stub.calls == 1 and cache.hits == 1


def test_key_is_normalized_case_and_trailing_dot():
    cache, stub, _ = make_cache(a_result())
    cache.resolve("Example.COM.", RecordType.A)
    cache.resolve("example.com", RecordType.A)
    assert stub.calls == 1


def test_different_record_types_are_cached_separately():
    cache, stub, _ = make_cache(a_result())
    cache.resolve("example.com", RecordType.A)
    cache.resolve("example.com", RecordType.AAAA)
    assert stub.calls == 2


# ---------------------------------------------------------------------------
# TTL rules
# ---------------------------------------------------------------------------

def test_positive_ttl_is_minimum_over_all_answers():
    result = make_result("www.github.com", RecordType.A, answers=[
        rr("www.github.com", RecordType.CNAME, 3600, "github.com"),
        rr("github.com", RecordType.A, 60, "20.207.73.82"),
    ])
    assert positive_ttl(result) == 60
    assert cache_ttl(result) == 60


def test_entry_expires_when_ttl_runs_out():
    cache, stub, clock = make_cache(a_result(ttl=60))
    cache.resolve("example.com", RecordType.A)
    clock.advance(59)
    cache.resolve("example.com", RecordType.A)   # still a hit
    assert stub.calls == 1
    clock.advance(1)                              # exactly 60s -> expired
    cache.resolve("example.com", RecordType.A)
    assert stub.calls == 2


def test_ttl_zero_is_not_cached():
    cache, stub, _ = make_cache(a_result(ttl=0))
    cache.resolve("example.com", RecordType.A)
    cache.resolve("example.com", RecordType.A)
    assert stub.calls == 2
    assert len(cache) == 0


def test_hit_reports_remaining_ttl():
    cache, _, clock = make_cache(a_result(ttl=60))
    cache.resolve("example.com", RecordType.A)
    clock.advance(25)
    hit = cache.resolve("example.com", RecordType.A)
    assert hit.answers[0].ttl == 35


def test_hit_has_zero_hops_and_empty_trace_and_does_not_change_stored_copy():
    original = a_result(ttl=60, queries=3)
    cache, _, clock = make_cache(original)
    cache.resolve("example.com", RecordType.A)
    clock.advance(10)
    hit = cache.resolve("example.com", RecordType.A)
    assert hit.hops == 0 and hit.trace == []
    assert original.answers[0].ttl == 60 and len(original.trace) == 3
    clock.advance(10)
    again = cache.resolve("example.com", RecordType.A)
    assert again.answers[0].ttl == 40  # counted from the original, not compounded


# ---------------------------------------------------------------------------
# Negative caching
# ---------------------------------------------------------------------------

def test_negative_ttl_uses_soa_minimum_when_smaller():
    result = nxdomain_result(soa_ttl=900, minimum=300)
    assert negative_ttl(result) == 300


def test_negative_ttl_uses_soa_ttl_when_smaller():
    result = nxdomain_result(soa_ttl=100, minimum=86400)
    assert negative_ttl(result) == 100


def test_nxdomain_is_cached_and_counted_as_negative_hit():
    cache, stub, _ = make_cache(nxdomain_result())
    cache.resolve("nope.com", RecordType.A)
    hit = cache.resolve("nope.com", RecordType.A)
    assert stub.calls == 1
    assert hit.rcode == ResponseCode.NXDOMAIN
    assert cache.negative_hits == 1 and cache.hits == 0


def test_nodata_is_cached():
    nodata = make_result("github.com", RecordType.AAAA, rcode=ResponseCode.NOERROR,
                         authority=[rr("github.com", RecordType.SOA, 900, soa_hex(86400))])
    cache, stub, clock = make_cache(nodata)
    cache.resolve("github.com", RecordType.AAAA)
    clock.advance(899)
    cache.resolve("github.com", RecordType.AAAA)
    assert stub.calls == 1
    clock.advance(1)
    cache.resolve("github.com", RecordType.AAAA)
    assert stub.calls == 2


def test_negative_answer_without_soa_is_not_cached():
    no_soa = make_result("nope.com", RecordType.A, rcode=ResponseCode.NXDOMAIN)
    cache, stub, _ = make_cache(no_soa)
    cache.resolve("nope.com", RecordType.A)
    cache.resolve("nope.com", RecordType.A)
    assert stub.calls == 2


def test_nxdomain_after_cname_uses_min_of_the_cnames():
    result = make_result("alias.com", RecordType.A, rcode=ResponseCode.NXDOMAIN,
                         answers=[rr("alias.com", RecordType.CNAME, 120, "gone.com")],
                         authority=[rr("com", RecordType.SOA, 900, soa_hex(900))])
    assert cache_ttl(result) == 120


# ---------------------------------------------------------------------------
# Errors are never cached
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error", [ResolutionError("all servers failed"),
                                   ValueError("bad name")])
def test_errors_are_raised_and_never_cached(error):
    cache, stub, _ = make_cache(error, a_result())
    with pytest.raises(type(error)):
        cache.resolve("example.com", RecordType.A)
    assert len(cache) == 0 and cache.errors == 1
    # the next call tries the resolver again and succeeds
    result = cache.resolve("example.com", RecordType.A)
    assert result.answers and stub.calls == 2


# ---------------------------------------------------------------------------
# LRU limit
# ---------------------------------------------------------------------------

def test_least_recently_used_entry_is_evicted():
    cache, stub, _ = make_cache(a_result(ttl=600), max_entries=2)
    cache.resolve("a.com", RecordType.A)
    cache.resolve("b.com", RecordType.A)
    cache.resolve("a.com", RecordType.A)        # touch a -> b is now oldest
    cache.resolve("c.com", RecordType.A)        # pushes b out
    assert cache.evictions == 1
    assert stub.calls == 3
    cache.resolve("a.com", RecordType.A)        # still cached
    assert stub.calls == 3
    cache.resolve("b.com", RecordType.A)        # was evicted -> miss
    assert stub.calls == 4


def test_max_entries_must_be_positive():
    with pytest.raises(ValueError):
        CachingResolver(resolver=StubResolver(a_result()), max_entries=0)


# ---------------------------------------------------------------------------
# stats, log, CSV, flush
# ---------------------------------------------------------------------------

def test_stats_counts_and_hit_ratio():
    cache, _, _ = make_cache(a_result())
    cache.resolve("example.com", RecordType.A)   # miss
    cache.resolve("example.com", RecordType.A)   # hit
    cache.resolve("example.com", RecordType.A)   # hit
    stats = cache.stats()
    assert stats["hits"] == 2 and stats["misses"] == 1
    assert stats["lookups"] == 3 and stats["size"] == 1
    assert stats["hit_ratio"] == pytest.approx(0.6667, abs=1e-4)


def test_log_has_one_row_per_lookup_with_query_counts():
    cache, _, _ = make_cache(a_result(queries=3))
    cache.resolve("example.com", RecordType.A)
    cache.resolve("example.com", RecordType.A)
    miss, hit = cache.log
    assert (miss.outcome, miss.queries_sent) == ("MISS", 3)
    assert (hit.outcome, hit.queries_sent) == ("HIT", 0)
    assert miss.name == "example.com" and miss.qtype == "A"


def test_csv_export(tmp_path):
    cache, _, _ = make_cache(a_result())
    cache.resolve("example.com", RecordType.A)
    cache.resolve("example.com", RecordType.A)
    path = tmp_path / "log.csv"
    assert cache.export_log_csv(path) == 2
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert [r["outcome"] for r in rows] == ["MISS", "HIT"]
    assert "latency_ms" in rows[0] and "queries_sent" in rows[0]


def test_flush_and_clear():
    cache, stub, _ = make_cache(a_result(ttl=600))
    cache.resolve("a.com", RecordType.A)
    cache.resolve("b.com", RecordType.A)
    cache.flush("A.com.")
    assert len(cache) == 1
    cache.resolve("a.com", RecordType.A)
    assert stub.calls == 3                       # a.com had to be fetched again
    cache.clear()
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# End-to-end: CachingResolver on top of Ayush's REAL IterativeResolver, using
# the fake network from tests/test_resolver.py (so real wire-format parsing,
# real SOA hex, real traces). Skipped automatically if that file isn't found.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_net():
    return pytest.importorskip("tests.test_resolver")


def test_e2e_second_lookup_sends_no_queries(fake_net):
    net = fake_net.standard_network()
    cache = CachingResolver(resolver=net.resolver(), clock=FakeClock())

    first = cache.resolve("example.com", RecordType.A)
    queries_after_miss = len(net.log)
    second = cache.resolve("Example.COM.", RecordType.A)

    assert queries_after_miss == 3 and len(first.trace) == 3
    assert len(net.log) == queries_after_miss      # the hit sent nothing
    assert second.trace == [] and second.addresses == first.addresses


def test_e2e_real_a_record_expires_after_its_ttl(fake_net):
    net = fake_net.standard_network()               # example.com A has ttl=60
    clock = FakeClock()
    cache = CachingResolver(resolver=net.resolver(), clock=clock)

    cache.resolve("example.com", RecordType.A)
    clock.advance(59)
    cache.resolve("example.com", RecordType.A)
    assert len(net.log) == 3
    clock.advance(1)
    cache.resolve("example.com", RecordType.A)
    assert len(net.log) > 3                        # cold walk again


def test_e2e_nxdomain_uses_real_soa_minimum(fake_net):
    # fake zone: SOA record ttl=900, MINIMUM=300 -> negative ttl must be 300
    net = fake_net.standard_network()
    clock = FakeClock()
    cache = CachingResolver(resolver=net.resolver(), clock=clock)

    first = cache.resolve("nope.example.com", RecordType.A)
    assert first.rcode == ResponseCode.NXDOMAIN
    assert cache_ttl(first) == 300
    queries = len(net.log)

    clock.advance(299)
    hit = cache.resolve("nope.example.com", RecordType.A)
    assert len(net.log) == queries and hit.rcode == ResponseCode.NXDOMAIN
    assert cache.negative_hits == 1

    clock.advance(1)
    cache.resolve("nope.example.com", RecordType.A)
    assert len(net.log) > queries


def test_e2e_resolution_failure_is_not_cached(fake_net):
    net = fake_net.standard_network(**{fake_net.COM: lambda qname, qtype: None})
    cache = CachingResolver(resolver=net.resolver(), clock=FakeClock())

    with pytest.raises(ResolutionError):
        cache.resolve("example.com", RecordType.A)
    assert len(cache) == 0 and cache.errors == 1
