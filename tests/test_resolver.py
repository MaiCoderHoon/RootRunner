"""
Tests for iterative resolution & server selection.

All tests run offline against a fake network: each fake server is a function
from (qname, qtype) to a response spec, and responses are encoded into real
DNS wire format so the resolver exercises DNSParser end to end.
"""

import random
import socket
import struct

import pytest

from src.dns_resolver.parser import (
    DNSEncoder, DNSHeader, DNSParser, DNSQuestion, RecordClass, RecordType, ResponseCode,
)
from src.dns_resolver.resolver import (
    HopLimitExceeded, IterativeResolver, ResolutionError, ServerSelector,
    build_query, is_subdomain,
)


# ---------------------------------------------------------------------------
# Fake network
# ---------------------------------------------------------------------------

def _name(name: str) -> bytes:
    out = b''
    for label in filter(None, name.split('.')):
        out += bytes([len(label)]) + label.encode('ascii')
    return out + b'\x00'


def _rdata(rtype: RecordType, value) -> bytes:
    if rtype == RecordType.A:
        return socket.inet_aton(value)
    if rtype in (RecordType.NS, RecordType.CNAME):
        return _name(value)
    if rtype == RecordType.SOA:
        return _name('ns.' + value) + _name('admin.' + value) + struct.pack('!IIIII', 1, 2, 3, 4, 300)
    raise NotImplementedError(rtype)


def rr(name, rtype, value, ttl=300):
    return (name, rtype, value, ttl)


def build_response(query: bytes, answers=(), authority=(), additional=(),
                   rcode=ResponseCode.NOERROR, aa=False, tc=False, id_delta=0, qname=None):
    header, questions, *_ = DNSParser().parse_message(query)
    q = questions[0]
    resp_header = DNSHeader(
        id=(header.id + id_delta) & 0xFFFF, qr=True, opcode=0, aa=aa, tc=tc, rd=False, ra=False,
        rcode=rcode, qdcount=1, ancount=len(answers), nscount=len(authority),
        arcount=len(additional),
    )
    echoed = DNSQuestion(qname or q.qname, q.qtype, RecordClass.IN)
    packet = DNSEncoder().encode_message(resp_header, [echoed])
    for name, rtype, value, ttl in (*answers, *authority, *additional):
        rdata = _rdata(rtype, value)
        packet += _name(name) + struct.pack('!HHIH', rtype, RecordClass.IN, ttl, len(rdata)) + rdata
    return packet


class FakeNetwork:
    """
    servers: {ip: handler(qname, qtype) -> dict of build_response kwargs | None}
    A handler returning None simulates a timeout. Every query is logged.
    """

    def __init__(self, servers, tcp_servers=None):
        self.servers = servers
        self.tcp_servers = tcp_servers or {}
        self.log = []

    def _handle(self, servers, transport, query, ip):
        _, questions, *_ = DNSParser().parse_message(query)
        q = questions[0]
        self.log.append((transport, ip, q.qname, q.qtype))
        handler = servers.get(ip)
        spec = handler(q.qname, q.qtype) if handler else None
        if spec is None:
            raise socket.timeout(f"fake timeout from {ip}")
        return build_response(query, **spec)

    def udp(self, query, ip, timeout):
        return self._handle(self.servers, 'udp', query, ip)

    def tcp(self, query, ip, timeout):
        return self._handle(self.tcp_servers, 'tcp', query, ip)

    def resolver(self, roots=('1.0.0.1',), **kw):
        kw.setdefault('selector', ServerSelector(rng=random.Random(0)))
        return IterativeResolver(root_servers=list(roots), udp=self.udp, tcp=self.tcp, **kw)


ROOT, COM, EXAMPLE, NET, CDN = '1.0.0.1', '2.0.0.1', '3.0.0.1', '4.0.0.1', '5.0.0.1'


def root_server(qname, qtype):
    if is_subdomain(qname, 'com'):
        return dict(authority=[rr('com', RecordType.NS, 'a.gtld.net')],
                    additional=[rr('a.gtld.net', RecordType.A, COM)])
    if is_subdomain(qname, 'net'):
        return dict(authority=[rr('net', RecordType.NS, 'a.gtld.net')],
                    additional=[rr('a.gtld.net', RecordType.A, NET)])
    return dict(rcode=ResponseCode.NXDOMAIN, aa=True, authority=[rr('', RecordType.SOA, 'root')])


def com_server(qname, qtype):
    if is_subdomain(qname, 'example.com'):
        return dict(authority=[rr('example.com', RecordType.NS, 'ns1.example.com')],
                    additional=[rr('ns1.example.com', RecordType.A, EXAMPLE)])
    return dict(rcode=ResponseCode.NXDOMAIN, aa=True, authority=[rr('com', RecordType.SOA, 'com')])


def example_server(qname, qtype):
    if qname == 'example.com' and qtype == RecordType.A:
        return dict(aa=True, answers=[rr('example.com', RecordType.A, '93.184.216.34', ttl=60)])
    if qname == 'www.example.com':
        return dict(aa=True, answers=[rr('www.example.com', RecordType.CNAME, 'example.com'),
                                      rr('example.com', RecordType.A, '93.184.216.34')])
    if qname == 'cdn.example.com':
        return dict(aa=True, answers=[rr('cdn.example.com', RecordType.CNAME, 'edge.cdn.net')])
    if qname == 'example.com':
        return dict(aa=True, authority=[rr('example.com', RecordType.SOA, 'example.com')])
    return dict(rcode=ResponseCode.NXDOMAIN, aa=True,
                authority=[rr('example.com', RecordType.SOA, 'example.com', ttl=900)])


def net_server(qname, qtype):
    if is_subdomain(qname, 'cdn.net'):
        return dict(authority=[rr('cdn.net', RecordType.NS, 'ns.cdn.net')],
                    additional=[rr('ns.cdn.net', RecordType.A, CDN)])
    return dict(rcode=ResponseCode.NXDOMAIN, aa=True)


def cdn_server(qname, qtype):
    return dict(aa=True, answers=[rr(qname, RecordType.A, '10.9.9.9')])


def standard_network(**overrides):
    servers = {ROOT: root_server, COM: com_server, EXAMPLE: example_server,
               NET: net_server, CDN: cdn_server}
    servers.update(overrides)
    return FakeNetwork(servers)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

class TestReferralWalk:
    def test_root_to_tld_to_authoritative(self):
        net = standard_network()
        result = net.resolver().resolve('example.com', RecordType.A)

        assert result.rcode == ResponseCode.NOERROR
        assert result.addresses == ['93.184.216.34']
        assert result.answers[0].ttl == 60
        assert result.hops == 3
        assert [ip for _, ip, _, _ in net.log] == [ROOT, COM, EXAMPLE]
        assert [s.outcome for s in result.trace] == ['referral', 'referral', 'answer']

    def test_queries_are_iterative(self):
        query = build_query('example.com', RecordType.A, 4242)
        header, questions, *_ = DNSParser().parse_message(query)
        assert header.rd is False
        assert header.id == 4242
        assert questions[0].qname == 'example.com'

    def test_name_is_normalized(self):
        result = standard_network().resolver().resolve('Example.COM.', RecordType.A)
        assert result.qname == 'example.com'
        assert result.addresses == ['93.184.216.34']

    def test_invalid_name_rejected(self):
        with pytest.raises(ValueError):
            standard_network().resolver().resolve('a' * 64 + '.com')


class TestCNAME:
    def test_in_answer_chain(self):
        result = standard_network().resolver().resolve('www.example.com', RecordType.A)
        assert [r.type for r in result.answers] == [RecordType.CNAME, RecordType.A]
        assert result.cname_chain == ['www.example.com', 'example.com']
        assert result.addresses == ['93.184.216.34']
        assert result.hops == 3

    def test_cross_zone_cname_reuses_learned_delegations(self):
        net = standard_network()
        result = net.resolver().resolve('cdn.example.com', RecordType.A)

        assert result.cname_chain == ['cdn.example.com', 'edge.cdn.net']
        assert result.addresses == ['10.9.9.9']
        # root, com, example.com, then root again for .net, net, cdn.net
        assert [ip for _, ip, _, _ in net.log] == [ROOT, COM, EXAMPLE, ROOT, NET, CDN]
        assert result.hops == 6

    def test_cname_query_type_returns_cname_itself(self):
        result = standard_network().resolver().resolve('cdn.example.com', RecordType.CNAME)
        assert [(r.type, r.data) for r in result.answers] == [(RecordType.CNAME, 'edge.cdn.net')]

    def test_cname_loop_detected(self):
        def looping(qname, qtype):
            other = 'b.example.com' if qname == 'a.example.com' else 'a.example.com'
            return dict(aa=True, answers=[rr(qname, RecordType.CNAME, other)])

        with pytest.raises(ResolutionError, match='loop'):
            standard_network(**{EXAMPLE: looping}).resolver().resolve('a.example.com')

    def test_long_cname_chain_hits_hop_limit(self):
        # c0 -> c1 -> c2 -> ... each answered separately: one hop per link
        def chain(qname, qtype):
            n = int(qname.split('.')[0][1:])
            return dict(aa=True, answers=[rr(qname, RecordType.CNAME, f'c{n + 1}.example.com')])

        with pytest.raises(HopLimitExceeded):
            standard_network(**{EXAMPLE: chain}).resolver().resolve('c0.example.com')


class TestNegativeAnswers:
    def test_nxdomain_keeps_soa_for_negative_caching(self):
        result = standard_network().resolver().resolve('nope.example.com', RecordType.A)
        assert result.rcode == ResponseCode.NXDOMAIN
        assert result.answers == []
        assert [(r.type, r.ttl) for r in result.authority] == [(RecordType.SOA, 900)]

    def test_nxdomain_from_tld(self):
        result = standard_network().resolver().resolve('missing.com', RecordType.A)
        assert result.rcode == ResponseCode.NXDOMAIN
        assert result.hops == 2

    def test_nodata(self):
        result = standard_network().resolver().resolve('example.com', RecordType.MX)
        assert result.rcode == ResponseCode.NOERROR
        assert result.answers == []
        assert result.authority[0].type == RecordType.SOA
        assert result.trace[-1].outcome == 'nodata'


# ---------------------------------------------------------------------------
# Failure handling & validation
# ---------------------------------------------------------------------------

class TestServerFailures:
    def two_server_com(self, first_handler):
        def root(qname, qtype):
            return dict(authority=[rr('com', RecordType.NS, 'a.gtld.net'),
                                   rr('com', RecordType.NS, 'b.gtld.net')],
                        additional=[rr('a.gtld.net', RecordType.A, '2.0.0.9'),
                                    rr('b.gtld.net', RecordType.A, COM)])
        net = standard_network(**{ROOT: root, '2.0.0.9': first_handler})
        resolver = net.resolver()
        # Make the bad server the first choice
        resolver.selector.srtt.update({'2.0.0.9': 1.0, COM: 50.0})
        return net, resolver

    @pytest.mark.parametrize('bad_handler, outcome', [
        (lambda q, t: None, 'timeout'),
        (lambda q, t: dict(rcode=ResponseCode.SERVFAIL), 'server-error'),
        (lambda q, t: dict(rcode=ResponseCode.REFUSED), 'server-error'),
        (lambda q, t: dict(id_delta=1), 'rejected'),
        (lambda q, t: dict(qname='evil.com'), 'rejected'),
    ])
    def test_falls_back_to_next_server(self, bad_handler, outcome):
        net, resolver = self.two_server_com(bad_handler)
        result = resolver.resolve('example.com')

        assert result.addresses == ['93.184.216.34']
        assert result.hops == 3                      # retries don't cost hops
        assert [s.outcome for s in result.trace] == ['referral', outcome, 'referral', 'answer']
        assert resolver.selector.srtt['2.0.0.9'] >= resolver.selector.failure_penalty_ms

    def test_all_servers_fail(self):
        net = FakeNetwork({})
        with pytest.raises(ResolutionError, match='no nameserver') as exc:
            net.resolver(roots=['1.0.0.1', '1.0.0.2']).resolve('example.com')
        assert [s.outcome for s in exc.value.trace] == ['timeout', 'timeout']

    def test_attempts_per_step_are_capped(self):
        roots = [f'1.0.0.{i}' for i in range(1, 14)]
        net = FakeNetwork({})
        with pytest.raises(ResolutionError):
            net.resolver(roots=roots, max_server_attempts=4).resolve('example.com')
        assert len(net.log) == 4

    def test_upward_referral_rejected(self):
        # A broken example.com server that refers back to the root
        def lame(qname, qtype):
            return dict(authority=[rr('com', RecordType.NS, 'a.gtld.net')],
                        additional=[rr('a.gtld.net', RecordType.A, COM)])

        with pytest.raises(ResolutionError, match='unusable response'):
            standard_network(**{EXAMPLE: lame}).resolver().resolve('example.com')

    def test_out_of_bailiwick_answer_ignored(self):
        # example.com's server tries to answer for a name in another zone
        def poisoner(qname, qtype):
            return dict(aa=True, answers=[rr('www.example.com', RecordType.CNAME, 'edge.cdn.net'),
                                          rr('edge.cdn.net', RecordType.A, '6.6.6.6')])

        net = standard_network(**{EXAMPLE: poisoner})
        result = net.resolver().resolve('www.example.com')
        assert result.addresses == ['10.9.9.9']           # fetched from the real cdn.net server
        assert '6.6.6.6' not in result.addresses

    def test_out_of_bailiwick_glue_ignored(self):
        # com server supplies glue for a .net name — not its zone to vouch for
        def com(qname, qtype):
            return dict(authority=[rr('example.com', RecordType.NS, 'ns.cdn.net')],
                        additional=[rr('ns.cdn.net', RecordType.A, '6.6.6.6')])

        def cdn(qname, qtype):
            if qname == 'ns.cdn.net':
                return dict(aa=True, answers=[rr('ns.cdn.net', RecordType.A, EXAMPLE)])
            return None

        net = standard_network(**{COM: com, CDN: cdn})
        result = net.resolver().resolve('example.com')
        assert result.addresses == ['93.184.216.34']
        assert '6.6.6.6' not in [ip for _, ip, _, _ in net.log]


class TestTruncation:
    def test_tc_bit_triggers_tcp_retry(self):
        net = standard_network(**{COM: lambda q, t: dict(tc=True)})
        net.tcp_servers = {COM: com_server}
        result = net.resolver().resolve('example.com')

        assert result.addresses == ['93.184.216.34']
        assert [(t, ip) for t, ip, _, _ in net.log] == [
            ('udp', ROOT), ('udp', COM), ('tcp', COM), ('udp', EXAMPLE)]
        assert 'TCP' in result.trace[1].detail

    def test_tcp_failure_falls_back_to_truncated_response(self):
        # Truncated UDP referral still carries usable NS + glue (common at the root)
        def truncated_com(qname, qtype):
            return dict(com_server(qname, qtype), tc=True)

        net = standard_network(**{COM: truncated_com})       # no TCP server => TCP times out
        result = net.resolver().resolve('example.com')
        assert result.addresses == ['93.184.216.34']


class TestGlueless:
    def test_glueless_ns_resolved_with_nested_lookup(self):
        def com(qname, qtype):
            if is_subdomain(qname, 'example.com'):
                return dict(authority=[rr('example.com', RecordType.NS, 'ns.cdn.net')])
            return None

        def cdn(qname, qtype):
            if qname == 'ns.cdn.net':
                return dict(aa=True, answers=[rr('ns.cdn.net', RecordType.A, EXAMPLE)])
            return None

        net = standard_network(**{COM: com, CDN: cdn})
        result = net.resolver().resolve('example.com')

        assert result.addresses == ['93.184.216.34']
        nested = [s for s in result.trace if s.depth == 1]
        assert [s.qname for s in nested] == ['ns.cdn.net'] * 3
        assert result.hops == 6                  # 3 main + 3 nested, one shared budget

    def test_glueless_cycle_does_not_recurse_forever(self):
        # example.com's only NS is inside example.com, but com gave no glue
        def com(qname, qtype):
            return dict(authority=[rr('example.com', RecordType.NS, 'ns1.example.com')])

        with pytest.raises(ResolutionError):
            standard_network(**{COM: com}).resolver().resolve('example.com')

    def test_hop_limit_counts_nested_lookups(self):
        def com(qname, qtype):
            if is_subdomain(qname, 'example.com'):
                return dict(authority=[rr('example.com', RecordType.NS, 'ns.cdn.net')])
            return None

        def cdn(qname, qtype):
            return dict(aa=True, answers=[rr(qname, RecordType.A, EXAMPLE)])

        net = standard_network(**{COM: com, CDN: cdn})
        with pytest.raises(HopLimitExceeded):
            net.resolver(max_hops=5).resolve('example.com')


# ---------------------------------------------------------------------------
# Server selection
# ---------------------------------------------------------------------------

class TestServerSelector:
    def test_prefers_lowest_srtt(self):
        sel = ServerSelector()
        sel.record_success('a', 80)
        sel.record_success('b', 10)
        sel.record_success('c', 40)
        assert sel.rank(['a', 'b', 'c']) == ['b', 'c', 'a']

    def test_unknown_servers_explored_before_slow_ones(self):
        sel = ServerSelector()
        sel.record_success('slow', 200)
        assert sel.rank(['slow', 'new']) == ['new', 'slow']

    def test_srtt_is_smoothed(self):
        sel = ServerSelector(alpha=0.5)
        sel.record_success('a', 100)
        sel.record_success('a', 20)
        assert sel.srtt['a'] == pytest.approx(60)

    def test_failure_penalizes(self):
        sel = ServerSelector(failure_penalty_ms=1000)
        sel.record_success('a', 10)
        sel.record_success('b', 50)
        sel.record_failure('a')
        assert sel.rank(['a', 'b']) == ['b', 'a']
        sel.record_failure('a')
        assert sel.srtt['a'] == 2000

    def test_rank_dedupes(self):
        assert sorted(ServerSelector().rank(['a', 'a', 'b'])) == ['a', 'b']

    def test_selector_state_persists_across_lookups(self):
        net = standard_network()
        resolver = net.resolver()
        resolver.resolve('example.com')
        assert set(resolver.selector.srtt) == {ROOT, COM, EXAMPLE}


@pytest.mark.parametrize('name, zone, expected', [
    ('www.example.com', 'example.com', True),
    ('example.com', 'example.com', True),
    ('example.com', '', True),
    ('badexample.com', 'example.com', False),
    ('com', 'example.com', False),
])
def test_is_subdomain(name, zone, expected):
    assert is_subdomain(name, zone) is expected
