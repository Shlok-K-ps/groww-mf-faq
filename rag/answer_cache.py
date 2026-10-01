"""Vetted answers for common concept / how-to questions, served instantly (no model call).

data/answer_cache.json holds PUBLIC data only: curated question phrasings + a reviewed answer + its source. It is built offline by
scripts/build_answer_cache.py from the real pipeline (every answer passes the validator) and reviewed by a human before commit.
User text is never written here. Lookup is exact on the normalised question, or near-exact (difflib ratio >= 0.9).
"""
import difflib
import json
import os
from functools import lru_cache

from rag.config import DATA_DIR
from rag.router import prep

PATH = os.path.join(DATA_DIR, "answer_cache.json")
NEAR = 0.90


@lru_cache(maxsize=4)
def _load(path):
    if not os.path.exists(path):
        return []
    entries = json.load(open(path, encoding="utf-8"))
    return [dict(e, _variants=[prep(q) for q in e["questions"]]) for e in entries]


def lookup(question, path=None):
    """-> entry dict {kind, answer, source_id, page, ...} or None. `question` is raw text that already passed the PII guard."""
    q = prep(question)
    best, best_ratio = None, 0.0
    for e in _load(path or PATH):
        for v in e["_variants"]:
            if v == q:
                return e
            r = difflib.SequenceMatcher(None, q, v).ratio()
            if r > best_ratio:
                best, best_ratio = e, r
    return best if best_ratio >= NEAR else None
