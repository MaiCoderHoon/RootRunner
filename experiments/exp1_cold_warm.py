# exp 1 - cold vs warm lookups
# resolve 20 sites once (cold, nothing cached) then again right after (warm, from cache)
# and compare the times. results go to experiments/results/exp1_cold_warm.csv
#
# run from project folder:  python -m experiments.exp1_cold_warm

import statistics
from datetime import datetime
from pathlib import Path

from src.dns_resolver.cache import CachingResolver
from src.dns_resolver.parser import RecordType
from src.dns_resolver.resolver import IterativeResolver, ResolutionError

sites = [
    "google.com", "github.com", "wikipedia.org", "python.org", "amazon.com",
    "microsoft.com", "apple.com", "cloudflare.com", "stackoverflow.com", "reddit.com",
    "netflix.com", "linkedin.com", "mozilla.org", "kiit.ac.in", "iitb.ac.in",
    "bbc.co.uk", "nytimes.com", "yahoo.com", "bing.com", "spotify.com",
]

out_dir = Path("experiments/results")


def do_pass(cache, label):
    before = len(cache.log)
    for site in sites:
        try:
            cache.resolve(site, RecordType.A)
        except (ResolutionError, ValueError) as err:
            print(f"[{label}] {site} failed: {err}")
    # only the rows added in this pass
    return cache.log[before:]


def show(rows, label):
    counts = {}
    for r in rows:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1

    # failed lookups dont have a real time so skip them
    times = [r.latency_ms for r in rows if r.outcome != "ERROR"]
    if not times:
        print(label, "-> no successful lookups")
        return None

    med = statistics.median(times)
    print(f"{label}: {counts}  median {med:.3f} ms  (min {min(times):.3f}, max {max(times):.3f})")
    return med


def main():
    # plain IterativeResolver = no delegation cache.
    # with it on, the cold pass would skip root hops after the first .com site
    cache = CachingResolver(IterativeResolver())

    cold = do_pass(cache, "cold")
    warm = do_pass(cache, "warm")

    cold_med = show(cold, "cold")
    warm_med = show(warm, "warm")
    if cold_med and warm_med:
        print(f"warm is about {cold_med / warm_med:,.0f}x faster (median)")

    out_dir.mkdir(parents=True, exist_ok=True)
    # date+time in the name so each run gets its own file
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    csv_path = out_dir / f"exp1_cold_warm_{stamp}.csv"
    n = cache.export_log_csv(csv_path)
    print(f"saved {n} rows -> {csv_path}")
    print(cache.stats())


if __name__ == "__main__":
    main()