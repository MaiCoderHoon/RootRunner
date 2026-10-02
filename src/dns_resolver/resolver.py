"""
Iterative DNS Resolution & Server Selection

Walks the DNS hierarchy starting at the root servers, the way a recursive
resolver does on a cache miss:

    root  --referral-->  TLD  --referral-->  authoritative  --answer-->

Implements:
- Referral following (authority NS + additional glue records)
- Glueless delegations (NS names resolved via a nested lookup)
- CNAME chain following (in-answer chains and cross-zone restarts)
- NXDOMAIN / NODATA detection (authority section kept for negative caching)
- TCP fallback when the TC bit is set
- Response validation: query ID, QR bit, echoed question, bailiwick checks
- RTT-based server selection (smoothed RTT per server IP, BIND-style)
- A global hop limit (MAX_HOPS) shared by referrals, CNAME restarts and
  glueless NS lookups

The wire format is handled entirely by parser.py; this module never touches
raw DNS bytes except to peek at the TC bit before a full parse.
"""

import argparse
import random
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.dns_resolver.parser import (
    DNSEncoder, DNSHeader, DNSParser, DNSQuestion, DNSRecord,
    RecordClass, RecordType, ResponseCode, format_rdata,
)


MAX_HOPS = 16
DEFAULT_TIMEOUT = 2.0          # seconds per server attempt
MAX_SERVER_ATTEMPTS = 6        # servers tried per step before giving up
UDP_BUFFER_SIZE = 4096
DNS_PORT = 53

# IPv4 addresses of a.root-servers.net .. m.root-servers.net
# Source: https://www.iana.org/domains/root/servers
ROOT_SERVERS = [
    "198.41.0.4",      # a.root-servers.net
    "170.247.170.2",   # b.root-servers.net
    "192.33.4.12",     # c.root-servers.net
    "199.7.91.13",     # d.root-servers.net
    "192.203.230.10",  # e.root-servers.net
    "192.5.5.241",     # f.root-servers.net
    "192.112.36.4",    # g.root-servers.net
    "198.97.190.53",   # h.root-servers.net
    "192.36.148.17",   # i.root-servers.net
    "192.58.128.30",   # j.root-servers.net
    "193.0.14.129",    # k.root-servers.net
    "199.7.83.42",     # l.root-servers.net
    "202.12.27.33",    # m.root-servers.net
]


class ResolutionError(Exception):
    """Resolution could not complete (all servers failed, lame delegation, ...)"""


class HopLimitExceeded(ResolutionError):
    """The walk needed more than MAX_HOPS steps (likely a loop)"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ResourceRecord:
    """A decoded resource record, ready for display or caching"""
    name: str
    type: RecordType
    ttl: int
    data: str                  # human-readable rdata (IP, target name, ...)


@dataclass
class TraceStep:
    """One server attempt during resolution — useful for debugging and experiments"""
    depth: int                 # 0 = main lookup, 1+ = nested glueless NS lookup
    hop: int
    zone: str                  # zone the server was believed authoritative for
    server: str
    qname: str
    qtype: RecordType
    outcome: str               # 'referral', 'answer', 'cname', 'nxdomain', 'timeout', ...
    rtt_ms: Optional[float] = None
    detail: str = ''


@dataclass
class ResolutionResult:
    qname: str
    qtype: RecordType
    rcode: ResponseCode
    answers: List[ResourceRecord]           # CNAME records (if any) followed by the final RRset
    authority: List[ResourceRecord]         # SOA etc. on NXDOMAIN / NODATA, for negative caching
    cname_chain: List[str]                  # names followed, e.g. ['www.x.com', 'x.cdn.net']
    hops: int
    trace: List[TraceStep] = field(default_factory=list)

    @property
    def addresses(self) -> List[str]:
        """Final A/AAAA values, convenience for callers that just want IPs"""
        return [r.data for r in self.answers if r.type in (RecordType.A, RecordType.AAAA)]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def send_udp(query: bytes, server: str, timeout: float) -> bytes:
    """
    Send a query over UDP and return the first reply from `server` whose ID
    matches the query. Stray datagrams (wrong source, wrong ID) are ignored
    until the timeout expires. Raises socket.timeout / OSError on failure.
    """
    query_id = query[:2]
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(query, (server, DNS_PORT))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout(f"no matching UDP reply from {server}")
            sock.settimeout(remaining)
            data, addr = sock.recvfrom(UDP_BUFFER_SIZE)
            if addr[0] == server and data[:2] == query_id:
                return data


def send_tcp(query: bytes, server: str, timeout: float) -> bytes:
    """Send a query over TCP (2-byte length prefix, RFC 1035 §4.2.2)"""
    with socket.create_connection((server, DNS_PORT), timeout=timeout) as sock:
        sock.sendall(struct.pack('!H', len(query)) + query)
        length = struct.unpack('!H', _recv_exact(sock, 2))[0]
        return _recv_exact(sock, length)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError(f"connection closed after {len(data)}/{n} bytes")
        data += chunk
    return data


Transport = Callable[[bytes, str, float], bytes]


# ---------------------------------------------------------------------------
# Server selection
# ---------------------------------------------------------------------------

class ServerSelector:
    """
    Ranks nameserver IPs by smoothed round-trip time (SRTT).

    - Servers never contacted get a small random SRTT, so they are explored
      before known-slow servers and load is spread across fresh server sets.
    - Successes update SRTT with an exponentially weighted moving average.
    - Failures (timeouts, errors, lame answers) double the SRTT, with a floor
      of `failure_penalty_ms`, pushing the server to the back of the queue.

    State persists for the lifetime of the selector, so one resolver instance
    learns which servers are fast across many lookups.
    """

    def __init__(self, alpha: float = 0.3, unknown_ms: float = 5.0,
                 failure_penalty_ms: float = 2000.0, rng: Optional[random.Random] = None):
        self.alpha = alpha
        self.unknown_ms = unknown_ms
        self.failure_penalty_ms = failure_penalty_ms
        self.rng = rng or random.Random()
        self.srtt: Dict[str, float] = {}

    def rank(self, servers: List[str]) -> List[str]:
        keys = {ip: self.srtt.get(ip, self.rng.uniform(0, self.unknown_ms)) for ip in servers}
        return sorted(dict.fromkeys(servers), key=keys.__getitem__)

    def record_success(self, server: str, rtt_ms: float) -> None:
        old = self.srtt.get(server)
        self.srtt[server] = rtt_ms if old is None else (1 - self.alpha) * old + self.alpha * rtt_ms

    def record_failure(self, server: str) -> None:
        self.srtt[server] = max(self.srtt.get(server, 0.0) * 2, self.failure_penalty_ms)


# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    return name.strip().rstrip('.').lower()


def is_subdomain(name: str, zone: str) -> bool:
    """True if `name` is `zone` or below it. The root zone is ''."""
    return zone == '' or name == zone or name.endswith('.' + zone)


def _validate_name(name: str) -> None:
    if len(name) > 253:
        raise ValueError(f"domain name too long: {name!r}")
    for label in name.split('.'):
        if not label or len(label) > 63:
            raise ValueError(f"invalid label in domain name: {name!r}")
        try:
            label.encode('ascii')
        except UnicodeEncodeError:
            raise ValueError(f"non-ASCII domain name (use punycode): {name!r}") from None


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

@dataclass
class _NameServer:
    name: str                       # NS host name ('' for root hints given as bare IPs)
    ips: List[str]                  # known IPv4 addresses (empty => glueless, resolve lazily)
    lookup_failed: bool = False


@dataclass
class _Response:
    header: DNSHeader
    answers: List[DNSRecord]
    authority: List[DNSRecord]
    additional: List[DNSRecord]
    packet: bytes
    server: str


class _Context:
    """Mutable state for one top-level resolve() call, shared with nested lookups"""

    def __init__(self, max_hops: int, root_servers: List[str]):
        self.max_hops = max_hops
        self.hops = 0
        self.depth = 0
        self.trace: List[TraceStep] = []
        # Delegations learned during this resolution: zone -> nameservers.
        # Lets CNAME restarts and glueless lookups skip steps already walked.
        self.delegations: Dict[str, List[_NameServer]] = {
            '': [_NameServer('', [ip]) for ip in root_servers]
        }
        self.active: Set[str] = set()   # NS names currently being resolved (cycle guard)

    def consume_hop(self, qname: str) -> int:
        if self.hops >= self.max_hops:
            raise HopLimitExceeded(f"exceeded {self.max_hops} hops resolving {qname}")
        self.hops += 1
        return self.hops

    def closest_zone(self, name: str) -> str:
        return max((z for z in self.delegations if is_subdomain(name, z)), key=len)


class IterativeResolver:
    """
    Iterative resolver that walks from the root servers to an authoritative answer.

        resolver = IterativeResolver()
        result = resolver.resolve('www.github.com', RecordType.A)
        print(result.addresses)

    Hop accounting: every step that sends a query to a zone's nameservers costs
    one hop (retries against other servers of the same zone are free). Steps
    taken after a CNAME and inside nested glueless NS lookups count against the
    same budget, so the whole walk is bounded by `max_hops`.
    """

    def __init__(self, root_servers: Optional[List[str]] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_hops: int = MAX_HOPS,
                 max_server_attempts: int = MAX_SERVER_ATTEMPTS,
                 selector: Optional[ServerSelector] = None,
                 udp: Transport = send_udp,
                 tcp: Transport = send_tcp):
        self.root_servers = list(root_servers or ROOT_SERVERS)
        self.timeout = timeout
        self.max_hops = max_hops
        self.max_server_attempts = max_server_attempts
        self.selector = selector or ServerSelector(failure_penalty_ms=timeout * 1000)
        self.udp = udp
        self.tcp = tcp

    # -- public API ---------------------------------------------------------

    def resolve(self, domain: str, record_type: RecordType = RecordType.A) -> ResolutionResult:
        qname = normalize_name(domain)
        _validate_name(qname)
        ctx = _Context(self.max_hops, self.root_servers)
        try:
            result = self._resolve(ctx, qname, RecordType(record_type))
        except ResolutionError as e:
            e.trace = ctx.trace         # keep the walk for debugging failed lookups
            raise
        result.trace = ctx.trace
        return result

    # -- core loop ----------------------------------------------------------

    def _resolve(self, ctx: _Context, qname: str, qtype: RecordType) -> ResolutionResult:
        """
        State machine for one name:
          ANSWER   -> return the RRset (plus any CNAMEs followed)
          CNAME    -> switch to the target name, restart from closest known zone
          REFERRAL -> descend into the child zone's nameservers
          NXDOMAIN / NODATA -> return negative result with authority section
        """
        current = qname
        chain_records: List[ResourceRecord] = []
        cname_chain = [qname]
        zone = ctx.closest_zone(current)

        while True:
            hop = ctx.consume_hop(current)
            resp = self._query_zone(ctx, hop, zone, current, qtype)

            # 1. Walk any CNAME chain inside the answer section (in-bailiwick only)
            target, final_rrs, new_cnames = self._follow_answer_chain(resp, current, qtype, zone)
            chain_records.extend(new_cnames)
            for name in cname_chain_names(new_cnames):
                if name in cname_chain:
                    raise ResolutionError(f"CNAME loop at {name} resolving {qname}")
                cname_chain.append(name)
            if final_rrs:
                self._trace_outcome(ctx, 'answer')
                return self._result(ctx, qname, qtype, ResponseCode.NOERROR,
                                    chain_records + final_rrs, [], cname_chain)
            if new_cnames:
                self._trace_outcome(ctx, 'cname', f"-> {target}")
                current = target
                zone = ctx.closest_zone(current)
                continue

            # 2. Negative answers
            authority = [self._decode(r, resp.packet) for r in resp.authority]
            if resp.header.rcode == ResponseCode.NXDOMAIN:
                self._trace_outcome(ctx, 'nxdomain')
                return self._result(ctx, qname, qtype, ResponseCode.NXDOMAIN,
                                    chain_records, authority, cname_chain)

            # 3. Referral to a child zone
            referral = self._extract_referral(resp, current, zone)
            if referral is not None:
                child_zone, nameservers = referral
                ctx.delegations[child_zone] = nameservers
                self._trace_outcome(ctx, 'referral', f"-> {child_zone or '.'} "
                                    f"({len(nameservers)} NS)")
                zone = child_zone
                continue

            # 4. Authoritative "name exists, but no records of this type"
            if resp.header.aa:
                self._trace_outcome(ctx, 'nodata')
                return self._result(ctx, qname, qtype, ResponseCode.NOERROR,
                                    chain_records, authority, cname_chain)

            raise ResolutionError(
                f"unusable response from {resp.server} for {current} in zone "
                f"'{zone or '.'}' (no answer, no referral, not authoritative)")

    def _result(self, ctx, qname, qtype, rcode, answers, authority, cname_chain):
        return ResolutionResult(qname=qname, qtype=qtype, rcode=rcode, answers=answers,
                                authority=authority, cname_chain=cname_chain, hops=ctx.hops)

    # -- talking to a zone's servers ---------------------------------------

    def _query_zone(self, ctx: _Context, hop: int, zone: str,
                    qname: str, qtype: RecordType) -> _Response:
        """
        Ask the nameservers of `zone` until one gives a usable response.
        Servers with known IPs are tried first (ranked by SRTT); glueless NS
        names are resolved lazily only if those all fail.
        """
        nameservers = ctx.delegations[zone]
        tried: Set[str] = set()
        attempts = 0

        def candidates() -> List[str]:
            ips = [ip for ns in nameservers for ip in ns.ips if ip not in tried]
            return self.selector.rank(ips)

        while attempts < self.max_server_attempts:
            ips = candidates()
            if not ips:
                if not self._resolve_one_glueless(ctx, nameservers):
                    break
                continue
            server = ips[0]
            tried.add(server)
            attempts += 1
            resp = self._query_server(ctx, hop, zone, server, qname, qtype)
            if resp is not None and self._is_usable(ctx, resp):
                return resp

        raise ResolutionError(
            f"no nameserver for zone '{zone or '.'}' answered usefully for {qname} "
            f"({attempts} attempt(s))")

    def _resolve_one_glueless(self, ctx: _Context, nameservers: List[_NameServer]) -> bool:
        """Resolve the address of the next glueless NS. Returns False if none are left."""
        for ns in nameservers:
            if ns.ips or ns.lookup_failed or not ns.name:
                continue
            if ns.name in ctx.active:
                ns.lookup_failed = True     # would recurse into itself
                continue
            ctx.active.add(ns.name)
            ctx.depth += 1
            try:
                result = self._resolve(ctx, ns.name, RecordType.A)
                ns.ips = [r.data for r in result.answers if r.type == RecordType.A]
            except HopLimitExceeded:
                raise
            except ResolutionError:
                pass
            finally:
                ctx.depth -= 1
                ctx.active.discard(ns.name)
            if not ns.ips:
                ns.lookup_failed = True
            return True
        return False

    def _query_server(self, ctx: _Context, hop: int, zone: str, server: str,
                      qname: str, qtype: RecordType) -> Optional[_Response]:
        """Send one query to one server; returns None (and records why) on failure."""
        query_id = secrets.randbits(16)
        query = build_query(qname, qtype, query_id)
        step = TraceStep(depth=ctx.depth, hop=hop, zone=zone, server=server,
                         qname=qname, qtype=qtype, outcome='pending')
        ctx.trace.append(step)

        start = time.monotonic()
        try:
            packet = self.udp(query, server, self.timeout)
            if _tc_bit_set(packet):
                step.detail = 'truncated, retried over TCP'
                try:
                    packet = self.tcp(query, server, self.timeout)
                except OSError:
                    step.detail = 'truncated, TCP retry failed; using partial UDP response'
        except OSError as e:
            self.selector.record_failure(server)
            step.outcome = 'timeout' if isinstance(e, socket.timeout) else 'network-error'
            step.detail = str(e)
            return None
        rtt_ms = (time.monotonic() - start) * 1000
        step.rtt_ms = rtt_ms

        try:
            header, questions, answers, authority, additional = DNSParser().parse_message(packet)
        except (ValueError, IndexError, struct.error, UnicodeDecodeError) as e:
            self.selector.record_failure(server)
            step.outcome = 'malformed'
            step.detail = f"could not parse response: {e}"
            return None

        problem = _validate_response(header, questions, query_id, qname, qtype)
        if problem:
            self.selector.record_failure(server)
            step.outcome = 'rejected'
            step.detail = problem
            return None

        self.selector.record_success(server, rtt_ms)
        return _Response(header, answers, authority, additional, packet, server)

    def _is_usable(self, ctx: _Context, resp: _Response) -> bool:
        """Server errors mean 'try another server', not 'the name is broken'."""
        if resp.header.rcode in (ResponseCode.NOERROR, ResponseCode.NXDOMAIN):
            return True
        self.selector.record_failure(resp.server)
        self._trace_outcome(ctx, 'server-error', resp.header.rcode.name)
        return False

    # -- interpreting responses --------------------------------------------

    def _follow_answer_chain(self, resp: _Response, qname: str, qtype: RecordType, zone: str
                             ) -> Tuple[str, List[ResourceRecord], List[ResourceRecord]]:
        """
        Walk qname through CNAMEs present in the answer section.

        Only records inside the responding server's zone (bailiwick) are
        trusted — a server for example.com must not be able to tell us the
        address of bank.com.

        Returns (name reached, final RRset for that name, CNAME records followed).
        """
        trusted = [r for r in resp.answers if is_subdomain(normalize_name(r.name), zone)]
        current = qname
        cnames: List[ResourceRecord] = []
        seen = {qname}

        while True:
            owned = [r for r in trusted if normalize_name(r.name) == current]
            final = [r for r in owned if r.type == qtype]
            if final:
                return current, [self._decode(r, resp.packet) for r in final], cnames
            cname = next((r for r in owned if r.type == RecordType.CNAME), None)
            if cname is None:
                return current, [], cnames
            decoded = self._decode(cname, resp.packet)
            cnames.append(decoded)
            current = normalize_name(decoded.data)
            if current in seen:
                return current, [], cnames      # loop; caught by caller's chain check
            seen.add(current)

    def _extract_referral(self, resp: _Response, qname: str, zone: str
                          ) -> Optional[Tuple[str, List[_NameServer]]]:
        """
        Find a delegation to a zone strictly below `zone` that still contains
        `qname`. Upward or sideways referrals are rejected (they cause loops).
        Glue is only accepted from the additional section if it lies inside
        the responding server's zone.
        """
        by_zone: Dict[str, List[str]] = {}
        for r in resp.authority:
            if r.type != RecordType.NS:
                continue
            owner = normalize_name(r.name)
            if owner != zone and is_subdomain(owner, zone) and is_subdomain(qname, owner):
                ns_name = normalize_name(self._decode(r, resp.packet).data)
                by_zone.setdefault(owner, []).append(ns_name)
        if not by_zone:
            return None

        child_zone = max(by_zone, key=len)      # most specific delegation
        glue: Dict[str, List[str]] = {}
        for r in resp.additional:
            name = normalize_name(r.name)
            if r.type == RecordType.A and is_subdomain(name, zone):
                glue.setdefault(name, []).append(self._decode(r, resp.packet).data)

        nameservers = [_NameServer(ns, glue.get(ns, []))
                       for ns in dict.fromkeys(by_zone[child_zone])]
        return child_zone, nameservers

    @staticmethod
    def _decode(record: DNSRecord, packet: bytes) -> ResourceRecord:
        # Always pass packet + offset: NS/CNAME/MX rdata may contain compression pointers
        try:
            data = format_rdata(record.type, record.rdata, packet=packet,
                                rdata_offset=record.rdata_offset)
        except (ValueError, IndexError, UnicodeDecodeError):
            data = record.rdata.hex()
        return ResourceRecord(normalize_name(record.name), record.type, record.ttl, data)

    @staticmethod
    def _trace_outcome(ctx: _Context, outcome: str, detail: str = '') -> None:
        step = ctx.trace[-1]
        step.outcome = outcome
        if detail:
            step.detail = f"{step.detail}; {detail}" if step.detail else detail


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def build_query(domain: str, record_type: RecordType, query_id: int) -> bytes:
    """Build an iterative (RD=0) query for one question"""
    header = DNSHeader(
        id=query_id, qr=False, opcode=0, aa=False, tc=False,
        rd=False,                   # iterative: we do the walking ourselves
        ra=False, rcode=ResponseCode.NOERROR,
        qdcount=1, ancount=0, nscount=0, arcount=0,
    )
    question = DNSQuestion(qname=domain, qtype=record_type, qclass=RecordClass.IN)
    return DNSEncoder().encode_message(header, [question])


def cname_chain_names(cname_records: List[ResourceRecord]) -> List[str]:
    return [normalize_name(r.data) for r in cname_records]


def _tc_bit_set(packet: bytes) -> bool:
    # Checked on raw bytes: a truncated packet may be cut mid-record and fail a full parse
    return len(packet) >= 3 and bool(packet[2] & 0x02)


def _validate_response(header: DNSHeader, questions: List[DNSQuestion],
                       query_id: int, qname: str, qtype: RecordType) -> Optional[str]:
    """Returns a reason string if the response doesn't belong to our query"""
    if header.id != query_id:
        return f"ID mismatch (sent {query_id}, got {header.id})"
    if not header.qr:
        return "QR bit not set (not a response)"
    if len(questions) != 1:
        return f"expected 1 question, got {len(questions)}"
    q = questions[0]
    if normalize_name(q.qname) != qname or q.qtype != qtype:
        return f"question mismatch (got {q.qname} {q.qtype.name})"
    return None


# ---------------------------------------------------------------------------
# CLI:  python -m src.dns_resolver.resolver www.github.com A --trace
# ---------------------------------------------------------------------------

def _print_trace(trace: List[TraceStep]) -> None:
    for s in trace:
        indent = '  ' * s.depth
        rtt = f"{s.rtt_ms:7.1f}ms" if s.rtt_ms is not None else '       --'
        zone = s.zone or '.'
        detail = f"  {s.detail}" if s.detail else ''
        print(f"{indent}[hop {s.hop:2}] {zone:<20} {s.server:<16} {rtt}  "
              f"{s.qname} {s.qtype.name}: {s.outcome}{detail}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Iterative DNS resolver (walks from the root)")
    ap.add_argument('name')
    ap.add_argument('type', nargs='?', default='A',
                    choices=[t.name for t in RecordType])
    ap.add_argument('--trace', action='store_true', help="show every server queried")
    ap.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)

    resolver = IterativeResolver(timeout=args.timeout)
    try:
        result = resolver.resolve(args.name, RecordType[args.type])
    except (ResolutionError, ValueError) as e:
        if args.trace and getattr(e, 'trace', None):
            _print_trace(e.trace)
        print(f"resolution failed: {e}")
        return 1

    if args.trace:
        _print_trace(result.trace)
        print()
    print(f";; {result.qname} {result.qtype.name}: {result.rcode.name}, {result.hops} hop(s)")
    for r in result.answers:
        print(f"{r.name:<30} {r.ttl:<7} {r.type.name:<6} {r.data}")
    if not result.answers:
        for r in result.authority:
            print(f"; authority: {r.name} {r.ttl} {r.type.name} {r.data}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
