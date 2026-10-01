"""PII guard. Runs FIRST on every user message: before routing, embedding, caching, logging or any API call.

Pure regex, no network. Returns category names only (never the matched text), so nothing sensitive can leak into logs.
Regex cannot catch every obfuscation (spelled-out digits, images, unusual separators); see README -> Known limits.
"""
import re
import unicodedata

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹘﹣－"), "-")


def normalize(text):
    """NFKC, native digits -> ASCII, strip zero-width chars, unify dashes, collapse whitespace."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(str(unicodedata.digit(c)) if unicodedata.category(c) == "Nd" else c for c in t)
    t = _INVISIBLE.sub("", t).translate(_DASHES)
    return re.sub(r"\s+", " ", t).strip()


def _join_groups(m):
    groups = re.split(r"[ \-.]", m.group(0))
    ok = all(2 <= len(g) <= 6 for g in groups) and sum(len(g) for g in groups) >= 9
    return "".join(groups) if ok else m.group(0)


def squeeze_digits(t):
    """'2345 6789 0123' / '98765-43210' -> contiguous digits. Only joins runs of >= 9 digits made of 2-6 digit groups,
    so dates (30 09 2026), decimals (1.69) and short amounts are left alone."""
    return re.sub(r"\d+(?:[ \-.]\d+)+", _join_groups, t)


_I = re.IGNORECASE
_VALUE4 = r"(?=[A-Za-z0-9*/\-]*\d)[A-Za-z0-9*/\-]{4,}"   # value with >= 1 digit (also masked: XXXX1234)
_VALUE6 = r"(?=[A-Za-z0-9*/\-]*\d)[A-Za-z0-9*/\-]{6,}"
_SEP = r"(?:\s*(?:no\.?|num(?:ber)?|id|card|code|is|are|was|:|-|#|=)){0,3}\s*[:\-#=]?\s*"

# (category, regex, applied-to) ; "sq" = digit-squeezed text, "t" = normalised text
_PATTERNS = [
    ("pan", re.compile(r"(?<![A-Z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Z0-9])", _I), "t"),
    ("aadhaar", re.compile(r"(?<!\d)[2-9]\d{11}(?!\d)"), "sq"),
    ("aadhaar", re.compile(r"(?<![A-Z0-9])(?:[X*]{4}[ \-]?){2}\d{4}(?!\d)", _I), "t"),
    ("phone", re.compile(r"(?<![\d])(?:(?:\+|00)\s?91[ \-]?|91[ \-]?|0)?[6-9]\d{9}(?!\d)"), "sq"),
    ("email", re.compile(r"[A-Z0-9._%+\-]+\s?(?:@|\(at\)|\[at\])\s?[A-Z0-9\-]+(?:\s?(?:\.|\(dot\)|\[dot\])\s?[A-Z0-9\-]+)*"
                         r"\s?(?:\.|\(dot\)|\[dot\])\s?[A-Z]{2,}", _I), "t"),
    ("otp", re.compile(r"\b(?:otp|one[ \-]?time[ \-]?(?:password|pin|code)?)\b\D{0,25}\d{4,8}(?!\d)", _I), "sq"),
    ("otp", re.compile(r"(?<!\d)\d{4,8}(?!\d)\D{0,15}\b(?:otp|one[ \-]?time[ \-]?(?:password|pin|code)?)\b", _I), "sq"),
    ("folio", re.compile(r"(?<![\d/])\d{5,}/\d{1,3}(?![\d/])"), "t"),
    ("labelled_id", re.compile(r"\b(?:pan|aadhaar|aadhar|uid|folio|otp|cvv)\b" + _SEP + _VALUE4, _I), "t"),
    ("labelled_id", re.compile(r"(?<![A-Za-z])(?:account|acct|acc|a/c|ifsc|pin)\b" + _SEP + _VALUE6, _I), "t"),
]


def scan(text):
    """Categories of PII found in `text` (empty list = clean). Never returns the matched values."""
    t = normalize(text)
    sq = squeeze_digits(t)
    found = []
    for cat, rx, on in _PATTERNS:
        if rx.search(sq if on == "sq" else t) and cat not in found:
            found.append(cat)
    return found


def contains_pii(text):
    return bool(scan(text))
