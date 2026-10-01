"""Check every URL in data/sources.csv is reachable.   python -m scripts.check_links

Tries HEAD first and falls back to a streamed GET (some hosts reject HEAD); follows redirects; checks the domain is allowlisted.
Exit code 1 if any source is unreachable, so it can run in CI or before a deploy.
"""
import csv
import sys
import time
from urllib.parse import urlparse

import requests

from rag.config import ALLOWED_DOMAINS

H = {"User-Agent": "Mozilla/5.0 (link check)"}


def check(url):
    t0 = time.perf_counter()
    method = "HEAD"
    try:
        r = requests.head(url, headers=H, timeout=30, allow_redirects=True)
        if r.status_code >= 400 or r.status_code == 405:
            raise requests.RequestException("HEAD refused")
    except requests.RequestException:
        method = "GET"
        r = requests.get(url, headers=H, timeout=60, allow_redirects=True, stream=True)
        r.close()
    size = r.headers.get("content-length")
    kind = (r.headers.get("content-type") or "").split(";")[0]
    return r.status_code, method, kind, size, r.url, int((time.perf_counter() - t0) * 1000)


def main():
    rows = list(csv.DictReader(open("data/sources.csv", encoding="utf-8")))
    bad = 0
    print(f"{'id':4} {'status':6} {'via':4} {'type':38} {'size':>9} {'ms':>6}  title")
    for r in rows:
        host = (urlparse(r["url"]).hostname or "").removeprefix("www.")
        try:
            status, method, kind, size, final, ms = check(r["url"])
            ok = status == 200 and host in ALLOWED_DOMAINS
            note = "" if urlparse(final).path == urlparse(r["url"]).path else f"  (redirects to {final})"
            print(f"{r['source_id']:4} {status:<6} {method:4} {kind[:38]:38} {(size or '-'):>9} {ms:>6}  {r['title'][:60]}{note}"
                  + ("" if host in ALLOWED_DOMAINS else "  !! DOMAIN NOT ALLOWLISTED"))
        except Exception as e:
            ok = False
            print(f"{r['source_id']:4} ERROR  {type(e).__name__}  {r['url']}")
        bad += not ok
    print(f"\n{len(rows) - bad}/{len(rows)} reachable" + ("" if not bad else f"  - {bad} PROBLEM(S)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
