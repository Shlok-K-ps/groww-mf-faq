"""Structured-fact answers from facts.json via code templates. No LLM call is ever made here.

One-citation rule: an answer covering several schemes is allowed only when a single source covers them all
(TER -> S10, riskometer -> S11). Every other field is per-scheme (KIM/SID) and needs a scheme.
"""
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from rag import sources
from rag.config import DATA_DIR, SCHEME_NAMES
from rag.validate import count_sentences

SHORT = {"Groww Large Cap Fund": "Large Cap", "Groww Multicap Fund": "Multicap",
         "Groww ELSS Tax Saver Fund": "ELSS Tax Saver", "Groww Small Cap Fund": "Small Cap"}
ALL_SCHEME_FIELDS = ("expense_ratio", "riskometer")        # covered by one source for every scheme
NOT_LIVE = "This is the latest published figure, not live data."


@dataclass
class FactAnswer:
    text: str = None
    source_id: str = None
    page: int = None
    needs_scheme: bool = False


@lru_cache(maxsize=1)
def load_facts():
    return json.load(open(os.path.join(DATA_DIR, "facts.json"), encoding="utf-8"))


def get(scheme, field, plan=None):
    for f in load_facts():
        if f["scheme"] == scheme and f["field"] == field and (plan is None or f["plan"] == plan):
            return f
    return None


def pct(v):
    return f"{v}%"


def one_line(cond):
    """Join a multi-line conditions text into one line without altering its words."""
    lines = [re.sub(r"^\s*[a-z]\)\s*", "", l.strip()) for l in (cond or "").split("\n") if l.strip()]
    if not lines:
        return ""
    out = lines[0]
    for l in lines[1:]:
        out += (" " if out[-1] in ".!?" else "; ") + l
    return re.sub(r"\s+", " ", out).strip()


def _ter(schemes, asks_current):
    rows = []
    for s in schemes:
        d, r = get(s, "expense_ratio", "direct"), get(s, "expense_ratio", "regular")
        if not d or not r:
            return None
        rows.append((s, d, r))
    date = sources.fmt_date(rows[0][1]["effective_date"])
    if len(rows) == 1:
        s, d, r = rows[0]
        text = (f"{s}'s total expense ratio (TER) is {pct(d['value'])} for the Direct plan and {pct(r['value'])} for the Regular "
                f"plan, as of {date}.")
    else:
        text = (f"Direct plan TERs as of {date}: " + ", ".join(f"{SHORT[s]} {pct(d['value'])}" for s, d, _ in rows) + ". "
                "Regular plan: " + ", ".join(pct(r["value"]) for _, _, r in rows) + " respectively.")
    if asks_current:
        text += " " + NOT_LIVE
    return FactAnswer(text, rows[0][1]["source_id"], rows[0][1]["page"])


def _risk(schemes):
    rows = [(s, get(s, "riskometer")) for s in schemes]
    if any(not f for _, f in rows):
        return None
    month = rows[0][1]["conditions"].replace("Riskometer level for ", "")
    if len(rows) == 1:
        s, f = rows[0]
        text = f"{s}'s riskometer level is {f['value']}, as of {month}."
    else:
        text = f"Riskometer levels as of {month}: " + ", ".join(f"{SHORT[s]} {f['value']}" for s, f in rows) + "."
    return FactAnswer(text, rows[0][1]["source_id"], rows[0][1]["page"])


def _per_scheme(scheme, field):
    f = get(scheme, field)
    if not f:
        return None
    cond = one_line(f["conditions"])
    if field == "exit_load":
        cond = re.sub(r"^Exit:?\s*", "", cond)
        if cond:
            text = f"{scheme}'s exit load, per its {sources.load()[f['source_id']]['doc_type']}: {cond.rstrip('.')}."
        else:
            text = f"{scheme} has an exit load of {f['value']}, per its KIM."
    elif field == "min_sip":
        text = f"{scheme}'s minimum SIP instalment is: {(cond or 'Rs. ' + f['value']).rstrip('.')}."
    elif field == "min_lumpsum":
        amount = cond if re.match(r"(Rs|₹|`|On continuous)", cond) else f"Rs. {f['value']}"
        text = f"{scheme}'s minimum lumpsum investment is: {amount}."
    elif field == "lock_in":
        if f["value"].lower().startswith("not specified"):
            text = f"{scheme} is an open-ended scheme, and its KIM does not specify a lock-in period."
        else:
            text = f"{scheme} has a statutory lock-in period of {f['value']}."
    elif field == "benchmark":
        text = f"{scheme}'s benchmark is {f['value']}."
    elif field == "fund_managers":
        text = f"{scheme}'s fund managers are {f['value'].replace('; ', ' and ')}, per its KIM."
    else:
        return None
    return FactAnswer(re.sub(r"\s+", " ", text), f["source_id"], f["page"])


def answer(field, schemes, asks_current=False):
    """FactAnswer for (field, schemes). needs_scheme=True -> ask the user which scheme. None -> not in facts.json (use RAG)."""
    if field == "expense_ratio":
        a = _ter(schemes or SCHEME_NAMES, asks_current)
    elif field == "riskometer":
        a = _risk(schemes or SCHEME_NAMES)
    elif len(schemes) == 1:
        a = _per_scheme(schemes[0], field)
    else:
        return FactAnswer(needs_scheme=True)        # none or several schemes: each comes from a different KIM
    if a and a.text and count_sentences(a.text) > 3:
        return None                                 # would break the 3-sentence rule -> fall back to RAG
    return a
