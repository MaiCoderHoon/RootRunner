# exp 5 - our query count vs dig +trace
# dig +trace walks root -> tld -> authoritative just like we do, and prints one
# ";; Received ... from <server>" line per answer it got. we count those lines.
#
# the FIRST received line is dig asking the home router (192.168.x.x) for the list
# of root servers. thats not part of the walk so we count it separately.
#
# note: dig +trace does NOT follow CNAMEs, it stops when it sees one.
# our resolver does follow them, so for CNAME names we will send more queries.
#
# run from project folder:  python -m experiments.exp5_vs_dig
# dig is expected at C:\dig\dig.exe, change DIG below if yours is elsewhere

import csv
import re
import subprocess
import time
from pathlib import Path

from src.dns_resolver.parser import RecordType
from src.dns_resolver.resolver import IterativeResolver, ResolutionError

DIG = r"C:\dig\dig.exe"
sites = [
    "google.com", "github.com", "wikipedia.org", "python.org", "amazon.com",
    "microsoft.com", "apple.com", "cloudflare.com", "stackoverflow.com", "reddit.com",
    "netflix.com", "linkedin.com", "mozilla.org", "kiit.ac.in", "iitb.ac.in",
    "bbc.co.uk", "nytimes.com", "yahoo.com", "bing.com", "spotify.com",
    "www.github.com", "www.microsoft.com",   # these two are CNAMEs
]
out_dir = Path("experiments/results")

received = re.compile(r";; Received \d+ bytes from ([^#\s]+)#\d+\(([^)]*)\) in (\d+) ms")


def run_dig(site):
    # -4 = ipv4 only (our resolver only uses ipv4), 2s timeout, 1 try, same as ours roughly
    cmd = [DIG, "-4", "+trace", "+nodnssec", "+time=2", "+tries=1", site, "A"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    hits = received.findall(out)
    if not hits:
        return None
    bootstrap, walk = hits[0], hits[1:]
    walk_ms = sum(int(ms) for _, _, ms in walk)
    servers = " > ".join(name for _, name, _ in walk)
    stopped_at_cname = bool(re.search(rf"^{re.escape(site)}\.\s+\d+\s+IN\s+CNAME", out, re.M))
    return len(walk), walk_ms, servers, stopped_at_cname


def run_ours(site):
    r = IterativeResolver()
    start = time.perf_counter()
    res = r.resolve(site, RecordType.A)
    ms = (time.perf_counter() - start) * 1000
    # cname_chain always starts with the name we asked for, so > 1 means we followed a cname
    followed = len(res.cname_chain) > 1
    # depth > 0 = extra lookups to find a nameserver's ip (no glue given)
    nested = sum(1 for s in res.trace if s.depth > 0)
    return len(res.trace), res.hops, ms, followed, nested


def main():
    rows = []
    print(f"{'site':20} {'dig q':>5} {'our q':>5} {'our hops':>8}  note")
    for site in sites:
        try:
            d = run_dig(site)
        except (OSError, subprocess.TimeoutExpired) as err:
            print(f"{site:20} dig failed: {err}")
            continue
        try:
            q, hops, ms, cname, nested = run_ours(site)
        except (ResolutionError, ValueError) as err:
            print(f"{site:20} our resolver failed: {err}")
            continue
        if d is None:
            print(f"{site:20} dig gave no output")
            continue
        dq, dms, servers, dig_cname = d
        notes = []
        if cname:
            notes.append("we followed a cname")
        if nested:
            notes.append(f"{nested} extra queries to find a nameserver ip (no glue)")
        note = ", ".join(notes)
        print(f"{site:20} {dq:5} {q:5} {hops:8}  {note}")
        rows.append([site, dq, round(dms), q, hops, round(ms), cname, nested, dig_cname, servers])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "exp5_vs_dig.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "dig_queries", "dig_ms", "our_queries", "our_hops", "our_ms",
                    "we_followed_cname", "glueless_queries", "dig_stopped_at_cname", "dig_servers"])
        w.writerows(rows)

    same = sum(1 for r in rows if r[1] == r[3])
    print(f"\nsame query count on {same} of {len(rows)} sites")
    print(f"saved -> {path}")


if __name__ == "__main__":
    main()