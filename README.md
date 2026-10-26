# RootRunner

**An iterative DNS resolver with caching, written from scratch in Python using only the standard library.**

RootRunner demonstrates how DNS resolution works beneath a typical recursive lookup. It starts with the root nameservers, follows referrals through TLD servers, and reaches authoritative nameservers. Positive and negative answers are cached until their TTL expires. RootRunner can also run as a local UDP DNS server.

~~~console
$ python -m src.dns_resolver.resolver github.com A
;; github.com A: NOERROR, 3 hop(s)
github.com                     60      A      20.207.73.82
~~~

A cold lookup commonly takes about three queries (root, TLD, authoritative) and a few hundred milliseconds. Add --trace to inspect queried servers and round-trip times. Results vary with the network and the time of the lookup.

## Contents

1. [Quick start](#1-quick-start)
2. [How it works](#2-how-it-works)
3. [Python API](#3-python-api)
4. [Local DNS server](#4-local-dns-server)
5. [Design decisions](#5-design-decisions)
6. [Tests](#6-tests)
7. [Experiments and results](#7-experiments-and-results)
8. [Known limitations](#8-known-limitations)
9. [Repository layout](#9-repository-layout)
10. [Team](#10-team)

---

## 1. Quick start

### Requirements

- Python 3 (developed and tested on Python 3.12 and 3.13).
- Internet access permitting outbound DNS traffic over UDP and TCP port 53.
- No third-party dependencies to run the resolver itself.
- pytest for tests; matplotlib and numpy only for regenerating experiment plots.

~~~bash
git clone https://github.com/MaiCoderHoon/RootRunner.git
cd RootRunner

python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt
~~~

Run commands from the repository root because the modules use src.dns_resolver imports.

~~~bash
python -m src.dns_resolver.resolver github.com
python -m src.dns_resolver.resolver github.com NS --trace
python -m src.dns_resolver.resolver doesnotexist-xyz123.com --trace
python -m pytest tests/ -q
~~~

Supported record types: A, AAAA, NS, CNAME, SOA, MX, and TXT.

---

## 2. How it works

### Architecture

~~~mermaid
flowchart TB
    Client["Client applications / OS"]
    Server["server.py<br/>Local UDP DNS server"]
    Cache["cache.py<br/>Answer cache + delegation cache"]
    Resolver["resolver.py<br/>IterativeResolver + ServerSelector"]
    Parser["parser.py<br/>DNSParser + DNSEncoder"]
    Hierarchy["DNS hierarchy<br/>Root → TLD → authoritative"]

    Client --> Server
    Server --> Cache
    Cache -->|Cache miss| Resolver
    Resolver <--> Parser
    Resolver <--> Hierarchy
    Cache -->|Cached answer| Server
~~~

| Layer | File | Responsibility |
|---|---|---|
| Wire format | src/dns_resolver/parser.py | DNS packet encoding and parsing, including compression pointers. |
| Resolution | src/dns_resolver/resolver.py | Referral walks, CNAME chains, glueless nameserver resolution, upstream TCP fallback, and RTT-aware server selection. |
| Caching | src/dns_resolver/cache.py | TTL-aware answer caching and delegation caching. |
| Server | src/dns_resolver/server.py | Local UDP DNS server backed by the caching resolver. |

### Resolution flow

A cold lookup of www.github.com starts at the root, follows the .com referral, and then queries the authoritative nameservers for github.com. The resolver sends iterative queries with RD=0.

~~~mermaid
sequenceDiagram
    participant App as Client
    participant Cache as CachingResolver
    participant Root as Root nameserver
    participant TLD as .com nameserver
    participant Auth as Authoritative nameserver

    App->>Cache: resolve(www.github.com, A)
    Cache->>Cache: Answer-cache lookup
    Note over Cache: Cache miss
    Cache->>Root: DNS query (RD=0)
    Root-->>Cache: Referral to .com
    Cache->>TLD: DNS query (RD=0)
    TLD-->>Cache: Referral to github.com
    Cache->>Auth: DNS query (RD=0)
    Auth-->>Cache: Answer or CNAME
    Cache->>Cache: Cache result using TTL
    Cache-->>App: ResolutionResult
~~~

| Response | Resolver action |
|---|---|
| Requested records | Return the answer, including CNAME records followed on the way. |
| CNAME | Follow the target name from the closest known zone. |
| NS records for a child zone | Follow the referral, using glue when available. |
| NXDOMAIN | Return the negative answer and SOA authority data for negative caching. |
| Authoritative answer without the requested type | Return NODATA, which can be cached. |
| Anything else | Raise ResolutionError. |

### Hop budget and server selection

Each query step to a zone's nameservers costs one hop. Retries against other servers in the same zone are free. CNAME restarts and nested glueless-nameserver lookups share the same budget. MAX_HOPS is 16; exceeding it raises HopLimitExceeded.

Each nameserver IP has a smoothed round-trip-time estimate (SRTT):

- New servers receive a small random initial estimate.
- Success updates the estimate using an exponentially weighted moving average (alpha = 0.3).
- Failure (timeout, invalid reply, or SERVFAIL) doubles the estimate, subject to a timeout floor, and moves the server to the back of the queue.
- Estimates persist for the lifetime of the resolver object.

### Caching

CachingResolver maintains two caches:

| Cache | Key → value | Lifetime | Effect |
|---|---|---|---|
| Answer cache | (name, type) → full result | Shortest answer TTL; negative answers use min(SOA TTL, SOA MINIMUM) following RFC 2308. | Repeat lookup can return without DNS queries. |
| Delegation cache | Zone → nameservers and glue | Shortest TTL of NS and glue records. | New lookups can skip root/TLD steps when valid delegation data is cached. |

- Cache hits report the remaining TTL.
- Errors are not cached: a timeout does not prove that a name does not exist.
- The answer cache uses LRU eviction and defaults to 1,000 entries.
- If cached delegation data appears stale, it is discarded and resolution retries from the root.
- Lookup logs include HIT, NEGATIVE_HIT, MISS, ERROR, latency, query count, and starting zone. Logs can be exported to CSV.

---

## 3. Python API

### Resolve a name

~~~python
from src.dns_resolver.resolver import IterativeResolver
from src.dns_resolver.parser import RecordType

resolver = IterativeResolver()
result = resolver.resolve("www.github.com", RecordType.A)

print(result.rcode.name)
print(result.addresses)
print(result.cname_chain)
print(result.hops)

for step in result.trace:
    print(step.zone, step.server, step.outcome, step.rtt_ms)
~~~

ResolutionResult exposes qname, qtype, rcode, answers, authority, cname_chain, hops, and trace. Each ResourceRecord has name, type, ttl, and a human-readable data value.

### Resolve with caching

~~~python
from src.dns_resolver.cache import CachingResolver
from src.dns_resolver.resolver import IterativeResolver

cache = CachingResolver()  # Answer cache + delegation cache
# cache = CachingResolver(IterativeResolver())  # Answer cache only

cache.resolve("github.com")       # MISS: starts from the root
cache.resolve("github.com")       # HIT: no DNS queries
cache.resolve("www.github.com")   # May use cached delegation information

print(cache.stats())
cache.export_log_csv("lookups.csv")
cache.flush("github.com")
cache.clear()
~~~

### Packet parsing

~~~python
from src.dns_resolver.parser import (
    DNSEncoder, DNSParser, DNSHeader, DNSQuestion,
    RecordType, RecordClass, ResponseCode, format_rdata,
)

header = DNSHeader(
    id=1234, qr=False, opcode=0, aa=False, tc=False, rd=False,
    ra=False, rcode=ResponseCode.NOERROR,
    qdcount=1, ancount=0, nscount=0, arcount=0,
)
query = DNSEncoder().encode_message(
    header,
    [DNSQuestion("google.com", RecordType.A, RecordClass.IN)],
)

# Send query over UDP and receive reply...
header, questions, answers, authority, additional = DNSParser().parse_message(reply)

for rec in answers:
    print(rec.name, rec.ttl, format_rdata(
        rec.type, rec.rdata, packet=reply, rdata_offset=rec.rdata_offset
    ))
~~~

**Compression-pointer gotcha:** NS, CNAME, and MX RDATA may refer to names elsewhere in the packet. Pass both packet and rdata_offset to format_rdata() to decode these names correctly. The iterative resolver does this.

### Exceptions

| Exception | Meaning |
|---|---|
| ResolutionError | Resolution could not complete (for example, all candidate servers failed, a lame delegation was encountered, or a CNAME loop was detected). Its trace records the attempted walk. |
| HopLimitExceeded | ResolutionError subclass raised when the 16-hop budget is exceeded. |
| ValueError | Invalid domain name, such as an empty label, label longer than 63 characters, name longer than 253 characters, or non-ASCII input. Convert internationalised names to punycode first. |

---

## 4. Local DNS server

The local server listens on UDP loopback addresses 127.0.0.1 and ::1 on port 53 by default. Port 53 may require elevated privileges; use an unprivileged port for initial testing.

~~~bash
python -m src.dns_resolver.server
python -m src.dns_resolver.server --port 5353
python -m src.dns_resolver.server --hosts 127.0.0.1
~~~

Test it with nslookup:

~~~bash
nslookup github.com 127.0.0.1
nslookup -port=5353 github.com 127.0.0.1
~~~

The second command is for a server running on port 5353. Stop with Ctrl+C. On shutdown, cache statistics are printed and the per-lookup log is saved to experiments/results/server_log.csv by default; use --log to choose another path.

### Behaviour and local browsing

- Failed resolutions return SERVFAIL; the server keeps running.
- Replies over 512 bytes are truncated by setting the TC bit.
- Unsupported query types such as HTTPS (65) and PTR (12) receive an empty NOERROR response.
- The local server supports UDP only. Upstream truncated replies are retried over TCP by the iterative resolver.

The project was validated for browsing on Windows 11. First verify the server with nslookup. If you configure Windows to use the local resolver, set IPv4 DNS to 127.0.0.1 and optionally IPv6 DNS to ::1. Chrome's Secure DNS setting may bypass the operating system resolver during testing. **Restore DNS assignment to Automatic before stopping RootRunner**, or name resolution may stop working after the server exits.

---

## 5. Design decisions

| Decision | Rationale |
|---|---|
| RD=0 on outgoing queries | RootRunner performs iterative resolution itself. |
| Bailiwick checks | Answer and glue data are accepted only within the relevant authority boundaries. |
| Reject upward and sideways referrals | Referrals must descend to a child zone that still contains the queried name, helping prevent loops. |
| Validate responses | Query ID, QR bit, and echoed question must match. IDs are generated using secrets. |
| Check TC before parsing the whole packet | A truncated packet can end mid-record; inspect the header and retry upstream over TCP. |
| Resolve glueless nameservers lazily | Resolve nameservers without known IPs only after known-address servers fail; nested lookups share the hop budget and use cycle protection. |
| Do not cache errors | A timeout or SERVFAIL is not a negative DNS answer. |
| Injectable transport and clock | IterativeResolver(udp=..., tcp=...) and CachingResolver(clock=...) enable offline tests and deterministic timing without sleep(). |
| Parse server questions separately | The server can handle unsupported query types without requiring the parser to support every record type. |

---

## 6. Tests

~~~bash
python -m pytest tests/ -q
python -m pytest tests/test_parser.py -v -s
~~~

| Test file | Coverage |
|---|---|
| tests/test_parser.py | DNS headers, names and questions, compression pointers, supported record types, encoder round trips, and real wire-format responses captured from 8.8.8.8 in docs/responses/*.bin. |
| tests/test_resolver.py | Referrals, CNAME chains and loops, NXDOMAIN/NODATA, retries, TCP fallback, glueless delegations, hop limits, and ServerSelector. Uses a fake network returning DNS wire-format packets. |
| tests/test_cache.py | TTL countdown, RFC 2308 negative caching, LRU eviction, error handling, delegation cache, stale-delegation retry, statistics, and CSV logging. Uses a stub resolver and fake clock. |
| tests/test_server.py | Question parsing, CNAME and AAAA replies, compression, cache hits, NXDOMAIN, SERVFAIL, unsupported types, malformed packets, and a local UDP socket round trip. |

The test suite includes **100+ tests**, designed to run without internet access.

To capture a fresh real DNS response for parser testing (requires network access):

~~~bash
python scripts/capture_real_response.py
~~~

---

## 7. Performance evaluation

RootRunner was evaluated through a set of repeatable performance tests and a real-world validation run from a home connection in October 2026. The full methodology, detailed tables, plots, and caveats are documented in [experiments/RESULTS.md](experiments/RESULTS.md). Raw CSVs are under experiments/results/ and plots are under experiments/plots/.

| # | Experiment | Headline result |
|---|---|---|
| 1 | Cold vs. warm latency across 20 sites | Cold median: **250–281 ms**; warm median: about **0.01 ms**. |
| 2 | 115 distinct sites, answer cache only vs. answer + delegation cache | Queries per lookup: **3.96 → 2.58**; median latency: **288 → 180 ms**. |
| 3 | 500 lookups over 50 sites with Zipf-like popularity | Cache hit ratio: **0.90** (best possible for this trace: 0.902). |
| 4 | Simulated unavailable nameservers | Resolution succeeds with 6 of 13 root servers unavailable. With all roots unavailable, it fails cleanly after 6 attempts rather than hanging. |
| 5 | Query count compared with dig +trace for 22 names | Counts matched for **19 of 22** names. Differences reflect glueless NS address resolution and cross-zone CNAME handling. |
| OS | Windows 11 browsing through server.py | Pages loaded normally; 207 filtered lookups, **0 errors**, hit ratio 0.39. |

Reproduce the experiments from the repository root:

~~~bash
python -m experiments.exp1_cold_warm
python -m experiments.exp2_many_sites
python -m experiments.exp3_replay_trace
python -m experiments.exp4_dead_server
python -m experiments.exp5_vs_dig
python -m experiments.make_plots
~~~

Experiment 5 requires dig; edit the executable path at the top of its script if needed. Regenerating plots requires matplotlib and numpy. Experiments 2–5 overwrite their CSVs when rerun, so measurements may differ; Experiment 1 saves each run to a dated file. The experiments query DNS servers starting at the root rather than forwarding queries to a public recursive resolver.

**Interpreting results:** The approximately 0.01 ms warm figure measures an in-process cache lookup, not a full DNS request over a socket. Experiments 2–5 are single runs on one network, not universal performance guarantees.

---

## 8. Known limitations

- **IPv4-only upstream transport.** Root and authoritative servers are queried over IPv4, and only A glue is used. AAAA record lookups are supported, but upstream DNS transport does not use IPv6.
- **No EDNS0 or DNSSEC.** Local server replies use a 512-byte UDP budget; the resolver can read upstream responses up to 4096 bytes.
- **UDP-only local server.** For responses larger than 512 bytes, the server sets TC and sends no records. A client retrying over TCP cannot receive the response because there is no local TCP listener.
- **Minimal response sections.** The server omits the authority section, so NXDOMAIN responses have no SOA. SOA records are not packed into server replies; unsupported types receive empty NOERROR.
- **Serial lookup processing.** The cache is not thread-safe, so lookups are serialized with a lock. A slow cold lookup delays other clients; this is intended for one machine, not a high-traffic resolver.
- **Limited record coverage.** Supported types are A, NS, CNAME, SOA, MX, TXT, and AAAA, class IN only. SOA data is shown as raw hexadecimal; IPv6 addresses are printed without zero compression.
- **Network-dependent failures.** In the test network, yandex.ru failed and westus-0.in.applicationinsights.azure.com returned SERVFAIL. The precise causes were not conclusively established.
- **Internal coupling.** DelegatingResolver uses IterativeResolver internals such as _Context, _NameServer, and _extract_referral. Renaming them may require updates in cache.py.

RootRunner is a hands-on implementation of DNS resolution internals and is not yet a hardened production DNS service. Avoid exposing it to untrusted networks.

---

## 9. Repository layout

~~~text
RootRunner/
├── src/dns_resolver/
│   ├── parser.py        # DNS wire format: DNSParser, DNSEncoder, format_rdata
│   ├── resolver.py      # IterativeResolver, ServerSelector, CLI
│   ├── cache.py         # CachingResolver, DelegatingResolver, DelegationCache
│   └── server.py        # Local UDP DNS server
├── tests/               # Offline unit tests
├── experiments/
│   ├── exp1_cold_warm.py
│   ├── exp2_many_sites.py
│   ├── exp3_replay_trace.py
│   ├── exp4_dead_server.py
│   ├── exp5_vs_dig.py
│   ├── make_plots.py
│   ├── filter_server_log.py
│   ├── results/         # CSVs and replay trace
│   ├── plots/           # PNG plots
│   ├── validation/      # Windows browsing validation screenshots
│   └── RESULTS.md       # Full experiment report
├── scripts/
│   └── capture_real_response.py
├── docs/responses/      # Captured DNS response packets
├── requirements.txt
└── pytest.ini
~~~

---

## 10. Contributors

| Component | Owner |
|---|---|
| DNS wire-format parser and encoder (parser.py) | Piyush Bisoi |
| Iterative resolution and server selection (resolver.py) | Ayush Kumar |
| Caching and TTL handling (cache.py) | Bibhu Biswal |
| Experiments, plots, server mode, and OS validation (experiments/, server.py) | Aryaman Padhi |

RootRunner implements core DNS behaviour informed by **RFC 1035** (DNS implementation and specification) and **RFC 2308** (negative caching). The root nameserver list is published by [IANA](https://www.iana.org/domains/root/servers).
