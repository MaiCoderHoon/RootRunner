# tests for server mode. no network: the cache sits on a stub resolver and
# replies are checked by parsing them back with Piyush's DNSParser.

import socket
import struct
import threading
from concurrent.futures import ThreadPoolExecutor

from src.dns_resolver.cache import CachingResolver
from src.dns_resolver.parser import DNSParser, RecordType, ResponseCode, format_rdata
from src.dns_resolver.resolver import ResolutionError, ResourceRecord, ResolutionResult, build_query
from src.dns_resolver.server import Server, build_reply, read_question, serve_socket


class Stub:
    def __init__(self, results):
        self.results = results   # name -> result or exception
        self.calls = 0

    def resolve(self, name, rtype=RecordType.A):
        self.calls += 1
        r = self.results[name]
        if isinstance(r, Exception):
            raise r
        return r


def rr(name, t, ttl, data):
    return ResourceRecord(name=name, type=t, ttl=ttl, data=data)


def result(name, answers=(), rcode=ResponseCode.NOERROR, qtype=RecordType.A):
    return ResolutionResult(qname=name, qtype=qtype, rcode=rcode, answers=list(answers),
                            authority=[], cname_chain=[name], hops=3, trace=[])


WWW = result("www.github.com", [rr("www.github.com", RecordType.CNAME, 3600, "github.com"),
                                rr("github.com", RecordType.A, 60, "20.207.73.82")])


def make_server(results):
    stub = Stub(results)
    return Server(cache=CachingResolver(resolver=stub), verbose=False), stub


def parse(reply):
    p = DNSParser()
    header, qs, ans, auth, add = p.parse_message(reply)
    shown = [(a.name, a.type, a.ttl, format_rdata(a.type, a.rdata, reply, a.rdata_offset)) for a in ans]
    return header, qs, shown


def test_cname_and_a_come_back_correctly():
    srv, _ = make_server({"www.github.com": WWW})
    header, qs, ans = parse(srv.handle(build_query("www.github.com", RecordType.A, 1234)))
    assert header.id == 1234 and header.qr and header.ra
    assert header.rcode == ResponseCode.NOERROR
    assert qs[0].qname == "www.github.com"
    assert ans == [("www.github.com", RecordType.CNAME, 3600, "github.com"),
                   ("github.com", RecordType.A, 60, "20.207.73.82")]


def test_reply_uses_compression_pointers():
    reply = build_reply(1, 0, True, "www.github.com", 1, 1, ResponseCode.NOERROR, WWW.answers)
    # answer names should point back into the question instead of repeating it
    assert b"\xc0\x0c" in reply
    assert reply.count(b"\x06github") == 1


def test_second_query_is_a_cache_hit():
    srv, stub = make_server({"www.github.com": WWW})
    srv.handle(build_query("www.github.com", RecordType.A, 1))
    srv.handle(build_query("www.github.com", RecordType.A, 2))
    assert stub.calls == 1


def test_nxdomain_passed_through():
    srv, _ = make_server({"nope.com": result("nope.com", rcode=ResponseCode.NXDOMAIN)})
    header, _, ans = parse(srv.handle(build_query("nope.com", RecordType.A, 7)))
    assert header.rcode == ResponseCode.NXDOMAIN and ans == []


def test_resolver_failure_gives_servfail():
    srv, _ = make_server({"dead.com": ResolutionError("no nameserver answered")})
    header, _, _ = parse(srv.handle(build_query("dead.com", RecordType.A, 9)))
    assert header.rcode == ResponseCode.SERVFAIL


def test_unknown_type_gets_empty_noerror():
    # HTTPS (65) isnt in our RecordType, browsers ask for it all the time
    q = struct.pack("!HHHHHH", 5, 0x0100, 1, 0, 0, 0) + b"\x06github\x03com\x00" + struct.pack("!HH", 65, 1)
    srv, stub = make_server({})
    reply = srv.handle(q)
    qid, flags, qd, an = struct.unpack("!HHHH", reply[:8])
    assert qid == 5 and flags & 0x8000 and flags & 0xF == 0 and an == 0
    assert stub.calls == 0


def test_aaaa_answer():
    res = result("x.com", [rr("x.com", RecordType.AAAA, 300, "2001:db8:0:0:0:0:0:1")], qtype=RecordType.AAAA)
    srv, _ = make_server({"x.com": res})
    _, _, ans = parse(srv.handle(build_query("x.com", RecordType.AAAA, 3)))
    assert ans[0][1] == RecordType.AAAA and ans[0][3] == "2001:db8:0:0:0:0:0:1"


def test_responses_and_junk_are_ignored():
    srv, _ = make_server({})
    assert srv.handle(b"\x00\x01") is None
    resp = struct.pack("!HHHHHH", 1, 0x8000, 0, 0, 0, 0)
    assert srv.handle(resp) is None


def test_read_question():
    q = build_query("GitHub.com", RecordType.A, 1)
    assert read_question(q)[1:] == (1, 1)


def test_over_real_udp_socket():
    srv, _ = make_server({"www.github.com": WWW})
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    pool = ThreadPoolExecutor(max_workers=2)
    threading.Thread(target=serve_socket, args=(s, srv, pool), daemon=True).start()

    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(3)
    c.sendto(build_query("www.github.com", RecordType.A, 4321), ("127.0.0.1", port))
    reply, _ = c.recvfrom(4096)
    header, _, ans = parse(reply)
    assert header.id == 4321 and ans[-1][3] == "20.207.73.82"
    c.close()
    s.close()