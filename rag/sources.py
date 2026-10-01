"""sources.csv access. Every URL a user ever sees comes from here (never from model output)."""
import csv
import datetime as dt
import os
from functools import lru_cache
from urllib.parse import urlparse

from rag.config import ALLOWED_DOMAINS, DATA_DIR


@lru_cache(maxsize=1)
def load():
    with open(os.path.join(DATA_DIR, "sources.csv"), encoding="utf-8") as f:
        return {r["source_id"]: r for r in csv.DictReader(f)}


def source_url(source_id, page=None):
    """URL from sources.csv; PDFs get a #page=N anchor when the page is known."""
    row = load()[source_id]
    url = row["url"]
    assert (urlparse(url).hostname or "").removeprefix("www.") in ALLOWED_DOMAINS, url
    if page and url.lower().endswith(".pdf"):
        url += f"#page={int(page)}"
    return url


def label(source_id):
    r = load()[source_id]
    base = f"{r['publisher']} · {r['doc_type']} · {r['title']}"
    return base + " · Excel file" if r["url"].lower().endswith((".xlsx", ".xls")) else base


def last_updated(source_id):
    """as_of_date of the source as 'DD Mon YYYY'."""
    d = dt.date.fromisoformat(load()[source_id]["as_of_date"])
    return d.strftime("%d %b %Y").lstrip("0")


def first_of(**match):
    """source_id of the first source whose columns equal `match` (e.g. doc_type='SID', scheme='Groww Large Cap Fund')."""
    for sid, r in load().items():
        if all(r.get(k) == v for k, v in match.items()):
            return sid
    return None


def fmt_date(iso):
    """'2026-09-30' -> '30 Sep 2026'."""
    return dt.date.fromisoformat(iso).strftime("%d %b %Y").lstrip("0")
