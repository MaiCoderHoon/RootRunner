# exp 3 - hit ratio over a replayed query trace
# real users dont ask for every site equally. a few sites (google, youtube) get asked
# a lot and most sites rarely. so we build a fake "day of browsing":
# 500 lookups picked from 50 sites, where popular sites come up much more often (zipf-ish).
# seed is fixed so the trace is the same every run.
# the trace is saved too, so anyone can replay the exact same thing.
#
# run from project folder:  python -m experiments.exp3_replay_trace

import random
from pathlib import Path

from src.dns_resolver.cache import CachingResolver
from src.dns_resolver.parser import RecordType
from src.dns_resolver.resolver import IterativeResolver, ResolutionError

# roughly most popular first
sites = [
    "google.com", "youtube.com", "facebook.com", "instagram.com", "wikipedia.org",
    "amazon.com", "github.com", "reddit.com", "linkedin.com", "netflix.com",
    "microsoft.com", "apple.com", "yahoo.com", "bing.com", "stackoverflow.com",
    "spotify.com", "twitch.tv", "discord.com", "zoom.us", "flipkart.com",
    "python.org", "mozilla.org", "cloudflare.com", "nytimes.com", "bbc.co.uk",
    "cnn.com", "medium.com", "quora.com", "pinterest.com", "imdb.com",
    "kiit.ac.in", "iitb.ac.in", "nptel.ac.in", "irctc.co.in", "zomato.com",
    "swiggy.com", "paypal.com", "ebay.com", "adobe.com", "dropbox.com",
    "notion.so", "figma.com", "canva.com", "slack.com", "gitlab.com",
    "docker.com", "npmjs.com", "kernel.org", "mit.edu", "stanford.edu",
]

TRACE_LEN = 500
SEED = 42
out_dir = Path("experiments/results")


def make_trace():
    # weight of site i is 1/(i+1): first site ~50x more likely than the last
    weights = [1 / (i + 1) for i in range(len(sites))]
    rng = random.Random(SEED)
    return rng.choices(sites, weights=weights, k=TRACE_LEN)


def main():
    trace = make_trace()
    print(f"trace: {len(trace)} lookups, {len(set(trace))} different sites")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "exp3_trace.txt").write_text("\n".join(trace) + "\n")

    cache = CachingResolver(IterativeResolver())
    for i, site in enumerate(trace, 1):
        try:
            cache.resolve(site, RecordType.A)
        except (ResolutionError, ValueError) as err:
            print(f"#{i} {site} failed: {err}")
        if i % 100 == 0:
            s = cache.stats()
            print(f"after {i}: hits {s['hits']}, misses {s['misses']}, hit ratio {s['hit_ratio']}")

    s = cache.stats()
    errors = s["errors"]
    ok_lookups = s["lookups"] - errors
    print("\nfinal:", s)
    print(f"hit ratio (all lookups)       = {s['hit_ratio']}")
    if ok_lookups:
        print(f"hit ratio (failed ones left out) = {(s['hits'] + s['negative_hits']) / ok_lookups:.4f}")

    # misses that happened on a site we had already seen = entry expired (TTL ran out)
    seen = set()
    expired = 0
    for r in cache.log:
        if r.outcome == "MISS" and r.name in seen:
            expired += 1
        seen.add(r.name)
    print(f"misses because the TTL ran out: {expired}")

    cache.export_log_csv(out_dir / "exp3_replay.csv")
    print(f"saved -> {out_dir / 'exp3_replay.csv'}")


if __name__ == "__main__":
    main()