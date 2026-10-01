"""Shared configuration: schemes, aliases, allowed domains, model names."""
import os

SCHEMES = {
    "Groww Large Cap Fund": ["largecap", "large cap", "large-cap"],
    "Groww Multicap Fund": ["multicap", "multi cap", "multi-cap"],
    "Groww ELSS Tax Saver Fund": ["elss", "tax saver", "tax-saver", "taxsaver", "80c", "tax saving"],
    "Groww Small Cap Fund": ["smallcap", "small cap", "small-cap"],
}
SCHEME_NAMES = list(SCHEMES)

ALLOWED_DOMAINS = {
    "growwmf.in", "assets-netstorage.growwmf.in", "cms-resources.growwmf.in",
    "amfiindia.com", "mutualfundssahihai.com", "sebi.gov.in", "investor.sebi.gov.in",
}

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768

DATA_DIR = "data"


_resolved = {}
_bad = set()


def mark_bad(name):
    """Skip a model that keeps failing (overloaded/retired) and force re-resolution."""
    _bad.add(name)
    _resolved.pop("m", None)


def resolve_model(client):
    """First usable model: GEMINI_MODEL, else the newest stable 'gemini-X.Y-flash'. No probe call (free tier is 20 req/day/model);
    callers report failures (429/503/404) with mark_bad() and ask again."""
    import re
    if "m" in _resolved:
        return _resolved["m"]
    found = []
    for m in client.models.list():
        mm = re.fullmatch(r"models/(gemini-(\d+(?:\.\d+)?)-flash)", m.name)
        if mm and "generateContent" in (getattr(m, "supported_actions", None) or []):
            found.append((float(mm.group(2)), mm.group(1)))
    avail = {m.name[7:] for m in client.models.list()}
    tail = [n for n in ("gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite") if n in avail]  # last-resort chain
    names = list(dict.fromkeys([GEMINI_MODEL] + [n for _, n in sorted(found, reverse=True)] + tail))
    names = [n for n in names if n not in _bad and n != "gemini-2.5-flash"]  # 2.5 retired for new keys
    if not names:
        raise RuntimeError("no Gemini model available")
    _resolved["m"] = names[0]
    return names[0]
