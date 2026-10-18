# server mode - lets windows (or any machine) use our resolver as its dns server.
# listens on udp port 53, reads the question, looks it up through the cache
# (which walks from the root on a miss) and sends the answer back.
#
# run from project folder:
#   python -m src.dns_resolver.server
# then set windows dns to 127.0.0.1 (see README / RESULTS for steps)
#
# every lookup is printed, and the cache log is saved to
# experiments/results/server_log.csv when you press ctrl+c

import argparse
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .cache import CachingResolver
from .parser import DNSEncoder, DNSHeader, RecordType, ResponseCode
from .resolver import ResolutionError

MAX_UDP = 512   # plain dns over udp, no edns


def read_question(packet):
    # walk the question by hand instead of using DNSParser, because browsers ask
    # for types our RecordType enum doesnt have (HTTPS=65, PTR=12) and the parser
    # would throw on those. we still want to answer them (with "no data").
    if len(packet) < 12:
        return None
    labels = []
    i = 12
    while True:
        if i >= len(packet):
            return None
        n = packet[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0:          # pointer inside a question, real clients never do this
            return None
        labels.append(packet[i + 1:i + 1 + n].decode("ascii", errors="replace"))
        i += 1 + n
    if i + 4 > len(packet):
        return None
    qtype, qclass = struct.unpack("!HH", packet[i:i + 4])
    return ".".join(labels), qtype, qclass


def pack_record(enc, rr):
    # one answer record. names go through Piyush's encode_name so they get
    # compression pointers. enc.packet must always hold everything written so far,
    # otherwise the pointer offsets come out wrong.
    if rr.type == RecordType.A:
        rdata = socket.inet_aton(rr.data)
    elif rr.type == RecordType.AAAA:
        rdata = socket.inet_pton(socket.AF_INET6, rr.data)
    elif rr.type == RecordType.TXT:
        txt = rr.data.encode()[:255]
        rdata = bytes([len(txt)]) + txt
    elif rr.type in (RecordType.CNAME, RecordType.NS, RecordType.MX):
        rdata = None   # has a name inside, written below
    else:
        return None    # SOA etc - skip

    name = enc.encode_name(rr.name.rstrip("."))
    enc.packet += name
    fixed_at = len(enc.packet)
    enc.packet += struct.pack("!HHIH", rr.type, 1, max(0, rr.ttl), 0)  # rdlength filled in below

    if rdata is None:
        if rr.type == RecordType.MX:
            pref, host = rr.data.split(None, 1)
            enc.packet += struct.pack("!H", int(pref))
            enc.packet += enc.encode_name(host.rstrip("."))
        else:
            enc.packet += enc.encode_name(rr.data.rstrip("."))
    else:
        enc.packet += rdata

    rdlen = len(enc.packet) - (fixed_at + 10)
    enc.packet = enc.packet[:fixed_at + 8] + struct.pack("!H", rdlen) + enc.packet[fixed_at + 10:]
    return True


def build_reply(qid, opcode, rd, qname, qtype, qclass, rcode, answers=()):
    enc = DNSEncoder()
    enc.packet = b"\x00" * 12      # space for the header, so offsets start at 12
    enc.name_offsets = {}
    enc.packet += enc.encode_name(qname.rstrip("."))
    enc.packet += struct.pack("!HH", qtype, qclass)
    question_end = len(enc.packet)

    count = 0
    for rr in answers:
        if pack_record(enc, rr):
            count += 1

    tc = False
    if len(enc.packet) > MAX_UDP:
        # too big for udp: send just the question with TC set, client should retry over tcp
        enc.packet = enc.packet[:question_end]
        count, tc = 0, True

    header = DNSHeader(id=qid, qr=True, opcode=opcode, aa=False, tc=tc, rd=rd, ra=True,
                       rcode=rcode, qdcount=1, ancount=count, nscount=0, arcount=0)
    return enc.encode_header(header) + enc.packet[12:]


class Server:
    def __init__(self, cache=None, verbose=True):
        # not 'cache or ...': an empty cache has len 0 so it counts as False
        self.cache = cache if cache is not None else CachingResolver()
        # the cache isnt thread safe, so only one lookup touches it at a time
        self.lock = threading.Lock()
        self.verbose = verbose

    def handle(self, packet):
        if len(packet) < 12:
            return None
        qid, flags = struct.unpack("!HH", packet[:4])
        if flags & 0x8000:          # that's a response, not a query
            return None
        opcode = (flags >> 11) & 0xF
        rd = bool(flags & 0x0100)
        q = read_question(packet)
        if q is None:
            return None
        qname, qtype, qclass = q

        if opcode != 0 or qclass != 1:
            return build_reply(qid, opcode, rd, qname, qtype, qclass, ResponseCode.NOTIMP)

        try:
            rtype = RecordType(qtype)
        except ValueError:
            # type we dont support -> "name may exist, but no records of that type"
            self.say(qname, f"type{qtype}", "UNSUPPORTED", 0, "NOERROR")
            return build_reply(qid, opcode, rd, qname, qtype, qclass, ResponseCode.NOERROR)

        start = time.perf_counter()
        try:
            with self.lock:
                result = self.cache.resolve(qname, rtype)
                outcome = self.cache.log[-1].outcome
        except (ResolutionError, ValueError) as err:
            self.say(qname, rtype.name, "ERROR", (time.perf_counter() - start) * 1000, f"SERVFAIL ({err})")
            return build_reply(qid, opcode, rd, qname, qtype, qclass, ResponseCode.SERVFAIL)
        except Exception as err:   # never let one bad lookup kill the server
            self.say(qname, rtype.name, "ERROR", (time.perf_counter() - start) * 1000, f"SERVFAIL ({err!r})")
            return build_reply(qid, opcode, rd, qname, qtype, qclass, ResponseCode.SERVFAIL)

        ms = (time.perf_counter() - start) * 1000
        self.say(qname, rtype.name, outcome, ms, result.rcode.name)
        return build_reply(qid, opcode, rd, qname, qtype, qclass, result.rcode, result.answers)

    def say(self, name, qtype, outcome, ms, rcode):
        if self.verbose:
            print(f"{time.strftime('%H:%M:%S')}  {name:40} {qtype:6} {outcome:12} {ms:8.1f} ms  {rcode}")


def serve_socket(sock, server, pool):
    sock.settimeout(1.0)
    while True:
        try:
            packet, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except ConnectionResetError:
            # windows quirk: a previous reply bounced (port unreachable). just ignore it
            continue
        except OSError:
            return

        def work(packet=packet, addr=addr):
            reply = server.handle(packet)
            if reply:
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    pass

        pool.submit(work)


def main(argv=None):
    ap = argparse.ArgumentParser(description="DNS server mode for our resolver")
    ap.add_argument("--port", type=int, default=53)
    ap.add_argument("--hosts", nargs="+", default=["127.0.0.1", "::1"],
                    help="addresses to listen on (default: 127.0.0.1 and ::1)")
    ap.add_argument("--log", default="experiments/results/server_log.csv")
    args = ap.parse_args(argv)

    server = Server()
    pool = ThreadPoolExecutor(max_workers=16)
    socks = []
    for host in args.hosts:
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_DGRAM)
        try:
            s.bind((host, args.port))
        except OSError as err:
            print(f"could not listen on [{host}]:{args.port} - {err}")
            s.close()
            continue
        socks.append(s)
        threading.Thread(target=serve_socket, args=(s, server, pool), daemon=True).start()
        print(f"listening on [{host}]:{args.port}")

    if not socks:
        print("nothing to listen on, quitting")
        return 1

    print("ctrl+c to stop\n")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for s in socks:
            s.close()
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        n = server.cache.export_log_csv(args.log)
        print(f"\nstopped. {server.cache.stats()}")
        print(f"saved {n} lookups -> {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())