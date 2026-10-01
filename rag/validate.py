"""Validator for model-written answers. Template-written fact answers are checked with the same sentence counter in tests.

A model answer passes only if: the cited source was retrieved, it has <= 3 sentences, no advice words, no URLs/HTML,
every number appears in the cited source's chunks, and any scheme it names is the asked scheme or appears in the cited text.
"""
import re

from rag.config import SCHEMES
from rag.router import detect_schemes

BANNED = re.compile(r"\b(recommend\w*|should|good choice|better|best|ideal|suitable|worth)\b", re.I)
_ABBR = re.compile(r"\b(Rs|Re|Mr|Ms|Mrs|Dr|No|Nos|vs|etc|approx|incl|i\.e|e\.g|p\.a|p\.m)\.", re.I)
_URLISH = re.compile(r"https?://|www\.|<[a-z/][^>]*>|\]\(", re.I)


def count_sentences(text):
    """Sentences split on . ? ! — ignoring decimals (0.65%), abbreviations (Rs. 500, Mr. X) and list markers (a)."""
    t = _ABBR.sub(lambda m: m.group(0).replace(".", "\x00"), text or "")
    t = re.sub(r"(?<=\d)\.(?=\d)", "\x00", t)
    t = re.sub(r"(?m)^\s*[a-z0-9]\)\s*", " ", t)
    parts = [p for p in re.split(r"(?<=[.!?])\s+", t.strip()) if re.search(r"\w", p)]
    return len(parts)


def numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", (text or "").replace(",", "")))


def validate(answer, source_id, retrieved, route_schemes=()):
    """Returns (ok, reasons). `retrieved` = chunk dicts that were given to the model."""
    why = []
    if not answer or not answer.strip():
        return False, ["empty"]
    cited = [c for c in retrieved if c["source_id"] == source_id]
    if not cited:
        why.append("citation not among retrieved sources")
    if count_sentences(answer) > 3:
        why.append("more than 3 sentences")
    if BANNED.search(answer):
        why.append("advice word")
    if _URLISH.search(answer):
        why.append("link or markup in answer")
    cited_text = " ".join(c["text"] for c in cited)
    missing = numbers(answer) - numbers(cited_text)
    if missing:
        why.append("number not in cited source: " + ",".join(sorted(missing)))
    low = cited_text.lower()
    for sch in detect_schemes(answer):
        if sch not in route_schemes and not any(a in low for a in [sch.lower()] + SCHEMES[sch]):
            why.append("names a scheme not asked about and absent from the cited source")
    return not why, why


def support_chunk(answer, source_id, retrieved):
    """The retrieved chunk of `source_id` that best supports the answer (most shared numbers/words) -> its page for #page=N."""
    cited = [c for c in retrieved if c["source_id"] == source_id]
    if not cited:
        return None
    toks = set(re.findall(r"[a-z0-9.]+", answer.lower()))
    nums = numbers(answer)
    return max(cited, key=lambda c: (len(nums & numbers(c["text"])), len(toks & set(re.findall(r"[a-z0-9.]+", c["text"].lower())))))
