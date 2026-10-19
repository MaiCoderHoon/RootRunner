# exp 2 - 100+ different sites, every one looked up only once
# no site repeats, so the answer cache cant help at all (hit ratio ~0).
# what can help is the delegation cache: it remembers who runs .com, .org etc
# so later lookups can skip the root (and sometimes the TLD).
# runs the same list twice: once without delegation cache, once with it.
#
# run from project folder:  python -m experiments.exp2_many_sites

import statistics
from pathlib import Path

from src.dns_resolver.cache import CachingResolver
from src.dns_resolver.parser import RecordType
from src.dns_resolver.resolver import IterativeResolver, ResolutionError

sites = [
    # .com
    "google.com", "youtube.com", "facebook.com", "amazon.com", "microsoft.com",
    "apple.com", "netflix.com", "linkedin.com", "twitter.com", "instagram.com",
    "github.com", "stackoverflow.com", "reddit.com", "yahoo.com", "bing.com",
    "spotify.com", "zoom.us", "dropbox.com", "adobe.com", "salesforce.com",
    "oracle.com", "ibm.com", "intel.com", "nvidia.com", "amd.com",
    "cisco.com", "paypal.com", "ebay.com", "walmart.com", "nytimes.com",
    "cnn.com", "forbes.com", "bloomberg.com", "medium.com", "quora.com",
    "pinterest.com", "tumblr.com", "twitch.tv", "discord.com", "slack.com",
    "notion.so", "figma.com", "canva.com", "airbnb.com", "uber.com",
    "booking.com", "expedia.com", "tripadvisor.com", "imdb.com", "hulu.com",
    "cloudflare.com", "akamai.com", "fastly.com", "digitalocean.com", "heroku.com",
    "gitlab.com", "bitbucket.org", "atlassian.com", "docker.com", "npmjs.com",
    # .org / .net / .edu / .io
    "wikipedia.org", "python.org", "mozilla.org", "apache.org", "linux.org",
    "debian.org", "ubuntu.com", "kernel.org", "gnu.org", "w3.org",
    "ietf.org", "icann.org", "iana.org", "archive.org", "khanacademy.org",
    "speedtest.net", "php.net", "sourceforge.net", "slideshare.net", "behance.net",
    "mit.edu", "stanford.edu", "harvard.edu", "berkeley.edu", "cmu.edu",
    "github.io", "itch.io", "socket.io", "angular.io", "repl.it",
    # country tlds
    "kiit.ac.in", "iitb.ac.in", "iitd.ac.in", "iisc.ac.in", "nptel.ac.in",
    "gov.in", "irctc.co.in", "flipkart.com", "zomato.com", "swiggy.com",
    "bbc.co.uk", "theguardian.com", "gov.uk", "ox.ac.uk", "cam.ac.uk",
    "spiegel.de", "lemonde.fr", "yandex.ru", "baidu.com", "naver.com",
    "rakuten.co.jp", "abc.net.au", "cbc.ca", "globo.com", "mercadolibre.com",
]

out_dir = Path("experiments/results")


def run(cache, label):
    for site in sites:
        try:
            cache.resolve(site, RecordType.A)
        except (ResolutionError, ValueError) as err:
            print(f"[{label}] {site} failed: {err}")

    rows = cache.log
    misses = [r for r in rows if r.outcome == "MISS"]
    errors = [r for r in rows if r.outcome == "ERROR"]

    # errors show 0 queries in the log, so leave them out of the averages
    q = [r.queries_sent for r in misses]
    t = [r.latency_ms for r in misses]
    from_root = sum(1 for r in misses if r.start_zone == ".")

    s = cache.stats()
    print(f"\n--- {label} ---")
    print(f"lookups {s['lookups']}, ok {len(misses)}, failed {len(errors)}, hit ratio {s['hit_ratio']}")
    print(f"queries per lookup: mean {statistics.mean(q):.2f}, median {statistics.median(q)}")
    print(f"latency: median {statistics.median(t):.1f} ms, mean {statistics.mean(t):.1f} ms")
    print(f"started at root: {from_root}, started lower down (delegation hits): {s['delegation_hits']}")

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"exp2_{label}.csv"
    cache.export_log_csv(csv_path)
    print(f"saved -> {csv_path}")


def main():
    print(f"{len(sites)} different sites, {len(set(sites))} unique")
    run(CachingResolver(IterativeResolver()), "answer_only")
    run(CachingResolver(), "with_delegation")


if __name__ == "__main__":
    main()