# keeps only the lookups from the sites we browsed on purpose (and their page assets)
# background apps (whatsapp, onedrive, steam...) are dropped so the log can go on github

import csv

src = "experiments/results/server_log.csv"
out = "experiments/results/server_log_browsing.csv"

keep = ["github.com", "githubassets.com", "wikipedia.org", "kiit.ac.in", "youtube.com",
        "ytimg.com", "google.com", "google.co.in", "gstatic.com", "googleapis.com",
        "doubleclick.net", "googlesyndication.com", "googleadservices.com",
        "googletagmanager.com", "google-analytics.com", "adtrafficquality.google",
        "facebook.com", "facebook.net", "cloudflareinsights.com", "cloudflare.com",
        "nel.goog", "gvt2.com"]


def wanted(name):
    return any(name == d or name.endswith("." + d) for d in keep)


with open(src, newline="") as f:
    rows = list(csv.DictReader(f))

mine = [r for r in rows if wanted(r["name"])]

with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(mine)


def show(label, rs):
    hits = sum(r["outcome"] in ("HIT", "NEGATIVE_HIT") for r in rs)
    errors = sum(r["outcome"] == "ERROR" for r in rs)
    print(f"{label}: {len(rs)} lookups, {hits} hits, {errors} errors, hit ratio {hits / len(rs):.4f}")


show("whole session", rows)
show("browsing only", mine)
print("saved", out)