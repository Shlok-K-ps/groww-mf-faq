"""Every user-visible response goes through render(). Code owns the format; the model only supplies an answer sentence.

kind: fact | concept | howto | advice | performance | mixed | clarify | not_found | out_of_scope | pii_block | service_unavailable
Source URL and 'Last updated' are always taken from sources.csv by source_id, never from model output.
render() takes no user text, so a refusal/PII notice can never echo the input.
"""
import re
from dataclasses import dataclass, field

from rag import sources
from rag.config import SCHEME_NAMES

SEBI_SID = sources.first_of(publisher="SEBI", doc_type="Education")
FACTSHEET_SID = sources.first_of(doc_type="Factsheet")

SCOPE_LINE = "Large Cap, Multicap, ELSS Tax Saver or Small Cap"

TEXT = {
    "advice": ("I can only share facts about mutual fund schemes, not advice on what to buy, sell or hold. For help deciding, you can "
               "read SEBI's investor guidance or speak with a SEBI-registered investment adviser."),
    "mixed_refusal": ("I can't advise on whether to invest or what suits you; for that, see SEBI's investor guidance or speak with a "
                      "SEBI-registered investment adviser."),
    "performance": ("I don't calculate or compare returns. You can see the official performance figures for {scheme} in Groww Mutual "
                    "Fund's latest factsheet."),
    "clarify": f"Which scheme do you mean? {SCOPE_LINE}.",
    "not_found": "I couldn't find this in my official sources.",
    "out_of_scope": ("I only cover these 4 Groww MF schemes and general mutual fund facts: " + ", ".join(SCHEME_NAMES) + "."),
    "pii_block": ("For your safety, please don't share personal details like PAN, Aadhaar, phone, email, OTP or account/folio numbers. "
                  "I don't need them and I don't store them. Ask your question without them and I'll help."),
    "service_unavailable": "Service busy, please try again in a moment.",
}
HIDDEN_USER_MESSAGE = "[message hidden: contained personal information]"

UI = {
    "title": "Groww MF Facts Assistant",
    "subtitle": "Facts from official Groww Mutual Fund, AMFI and SEBI sources.",
    "banner_head": "Facts-only. No investment advice.",
    "banner_body": "Answers come from official public documents and may lag recent updates. Verify using the source link.",
    "welcome": ("Hi! Ask me factual questions about 4 Groww Mutual Fund schemes: Largecap, Multicap, ELSS Tax Saver and Small Cap."),
    "examples": ["What is the expense ratio of Groww Large Cap Fund (Direct)?",
                 "What is the lock-in period for Groww ELSS Tax Saver Fund?",
                 "How do I download my capital-gains statement?"],
    "footer": "Student prototype. Not affiliated with Groww.",
}


@dataclass
class Response:
    kind: str
    text: str
    source_id: str = None
    source_url: str = None
    source_label: str = None
    last_updated: str = None
    refusal_url: str = None        # separate "Learn more" link for refusals (never the factual citation)
    refusal_text: str = None       # second paragraph for kind="mixed"
    fields: dict = field(default_factory=dict)

    @property
    def cited(self):
        return self.source_url is not None


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.I)
_TAG = re.compile(r"<[^>]+>")


def strip_links(text):
    """Remove markdown links (keep the label), bare URLs and HTML from model output."""
    t = _MD_LINK.sub(r"\1", text or "")
    t = _TAG.sub("", t)
    t = _URL.sub("", t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def _cite(resp, source_id, page=None):
    resp.source_id = source_id
    resp.source_url = sources.source_url(source_id, page)
    resp.source_label = sources.label(source_id)
    resp.last_updated = sources.last_updated(source_id)
    return resp


def render(kind, *, answer=None, source_id=None, page=None, scheme=None, closest_source_id=None):
    """The one place responses are built. `answer` is code-templated or model-supplied text (links stripped here)."""
    if kind in ("fact", "concept", "howto"):
        return _cite(Response(kind, strip_links(answer)), source_id, page)
    if kind == "mixed":
        r = _cite(Response(kind, strip_links(answer), refusal_text=TEXT["mixed_refusal"]), source_id, page)
        r.refusal_url = sources.source_url(SEBI_SID)
        return r
    if kind == "advice":
        return Response(kind, TEXT["advice"], refusal_url=sources.source_url(SEBI_SID))
    if kind == "performance":
        r = Response(kind, TEXT["performance"].format(scheme=scheme or "Groww Mutual Fund's schemes"))
        return _cite(r, FACTSHEET_SID)
    if kind == "not_found":
        r = Response(kind, TEXT["not_found"])
        if closest_source_id:   # point at the most relevant official document instead of guessing
            r.source_id = closest_source_id
            r.source_url = sources.source_url(closest_source_id)
            r.source_label = sources.label(closest_source_id)
        return r
    if kind in ("clarify", "out_of_scope", "pii_block", "service_unavailable"):
        return Response(kind, TEXT[kind])
    raise ValueError(f"unknown response kind: {kind}")


def to_text(r):
    """Plain-text rendering of the PRD answer format (used by the CLI, eval and tests; the UI renders the same fields)."""
    lines = [r.text]
    if r.kind == "mixed":
        lines.append(r.refusal_text)
        lines.append(f"Learn more: {r.refusal_url}")
    if r.kind == "advice":
        lines.append(f"Learn more: {r.refusal_url}")
    if r.cited and r.kind != "not_found":
        lines.append(f"Source: {r.source_url}")
        lines.append(f"Last updated from sources: {r.last_updated}")
    elif r.kind == "not_found" and r.source_url:
        lines.append(f"Closest source: {r.source_url}")
    return "\n".join(lines)
