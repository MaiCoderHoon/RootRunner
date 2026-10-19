# exp 4 - what happens when a server is unreachable
# we cant switch off real servers, so we wrap the udp send function:
# if the server ip is in the "dead" set, we wait the full timeout and then
# raise socket.timeout, exactly like a server that never replies.
#
# scenarios for each site:
#   normal        - nothing dead
#   auth_dead     - the authoritative server that answered in "normal" is dead
#   half_roots    - 6 of the 13 root servers are dead
#   all_roots     - every root server is dead (lookup should fail, not hang)
#
# run from project folder:  python -m experiments.exp4_dead_server

import csv
import socket
import time
from pathlib import Path

from src.dns_resolver.parser import RecordType
from src.dns_resolver.resolver import (IterativeResolver, ResolutionError,
                                       ROOT_SERVERS, send_udp)

sites = ["github.com", "wikipedia.org", "python.org", "kiit.ac.in"]
TIMEOUT = 1.0   # 1s per attempt so the runs dont take forever
out_dir = Path("experiments/results")


def make_udp(dead):
    def udp(query, server, timeout):
        if server in dead:
            time.sleep(timeout)
            raise socket.timeout(f"{server} is dead (simulated)")
        return send_udp(query, server, timeout)
    return udp


def lookup(site, dead):
    # new resolver every time so no server rtt memory carries over
    r = IterativeResolver(timeout=TIMEOUT, udp=make_udp(dead))
    start = time.perf_counter()
    try:
        res = r.resolve(site, RecordType.A)
        ms = (time.perf_counter() - start) * 1000
        timeouts = sum(1 for s in res.trace if s.outcome == "timeout")
        return res, "ok", ms, len(res.trace), timeouts, ""
    except ResolutionError as err:
        ms = (time.perf_counter() - start) * 1000
        return None, "failed", ms, 0, 0, str(err)


def main():
    rows = []
    for site in sites:
        res, status, ms, q, t, err = lookup(site, set())
        rows.append([site, "normal", status, round(ms, 1), q, t, ""])
        print(f"{site:15} normal      {status:6} {ms:8.1f} ms  queries {q}  timeouts {t}")
        if res is None:
            continue

        # the server that gave the final answer
        auth_ip = [s.server for s in res.trace if s.outcome == "answer"][-1]

        scenarios = {
            "auth_dead": {auth_ip},
            "half_roots": set(ROOT_SERVERS[:6]),
            "all_roots": set(ROOT_SERVERS),
        }
        for name, dead in scenarios.items():
            _, status, ms, q, t, err = lookup(site, dead)
            rows.append([site, name, status, round(ms, 1), q, t, err])
            print(f"{site:15} {name:11} {status:6} {ms:8.1f} ms  queries {q}  timeouts {t}  {err}")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "exp4_dead_server.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "scenario", "status", "latency_ms", "queries", "timeouts", "error"])
        w.writerows(rows)
    print(f"saved -> {path}")


if __name__ == "__main__":
    main()