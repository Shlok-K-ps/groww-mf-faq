"""Question -> Response. Order is fixed:  PII guard -> (session cache) -> route -> facts templates | RAG+Gemini -> validate -> render.

Privacy rules enforced here:
  * pii.scan() runs before routing, embedding, caching or any API call; a blocked message goes nowhere else.
  * the only log line is {intent, latency_ms, source_id, blocked}; exception handlers log the exception *type* only.
  * the optional per-session `cache` is supplied by the caller (Streamlit session_state), keyed by the normalised question,
    and never holds blocked messages.
"""
import json
import logging
import time
from dataclasses import dataclass

from rag import answer_cache, factsqa, pii, sources
from rag.config import SCHEME_NAMES
from rag.generate import generate
from rag.llm import LLM, Budget, LLMUnavailable
from rag.retrieve import expand_terms, retrieve
from rag.router import prep, route
from rag.templates import FIELD_BUTTONS, UI, render, strip_links
from rag.validate import support_chunk, validate

log = logging.getLogger("gmf")


@dataclass
class Result:
    response: object
    intent: str
    latency_ms: int
    blocked: bool = False
    via: str = "rules"      # rules | facts | rag | cache | llm-classifier
    cached: bool = False


def log_event(intent, latency_ms, source_id, blocked):
    """The ONLY thing ever logged about a request. No text, no hashes of text."""
    log.info(json.dumps({"intent": intent, "latency_ms": latency_ms, "source_id": source_id, "blocked": blocked}))


def fill_scheme(question, scheme):
    """Used by the UI's scheme buttons: resend the original question with the chosen scheme filled in."""
    return f"{question.strip().rstrip('?')} for {scheme}?"


class Assistant:
    def __init__(self, llm=None, use_llm=True):
        self.llm = (llm or LLM()) if use_llm else None

    # ------------------------------------------------------------ public
    def ask(self, text, cache=None):
        t0 = time.perf_counter()
        blocked, via, cached = False, "rules", False
        try:
            resp, intent, via, blocked, cached = self._ask(text, cache)
        except LLMUnavailable:
            resp, intent = render("service_unavailable"), "error"
        except Exception as e:                  # never log the message or the exception text (may embed the prompt)
            log.error("pipeline_error type=%s", type(e).__name__)
            resp, intent = render("service_unavailable"), "error"
        ms = int((time.perf_counter() - t0) * 1000)
        log_event(intent, ms, resp.source_id, blocked)
        return Result(resp, intent, ms, blocked, via, cached)

    # ------------------------------------------------------------ internals
    def _classifier(self, prompt):
        return self.llm.json_once(prompt, timeout=3.0)

    def _ask(self, text, cache):
        if len(text) > pii.MAX_CHARS:                                  # refuse without processing: could hide anything
            return render("too_long"), "too_long", "rules", True, False
        if pii.contains_pii(text):                                     # 1. before everything else
            return render("pii_block"), "pii", "rules", True, False
        key = prep(text)
        if cache is not None and key in cache:
            resp, intent = cache[key]
            return resp, intent, "cache", False, True
        r = route(text, self._classifier if self.llm else None)        # 2. rules first, LLM only if unconfident
        resp, via = self._dispatch(text, r)
        if cache is not None and resp.kind != "service_unavailable":
            cache[key] = (resp, r.intent)
        return resp, r.intent, via, False, False

    def _dispatch(self, text, r):
        i = r.intent
        if i == "advice":
            return render("advice"), "rules"
        if i == "performance_calc":
            return render("performance", scheme=r.schemes[0] if len(r.schemes) == 1 else None), "rules"
        if i == "out_of_scope":
            return render("out_of_scope"), "rules"
        if i == "unsure":                                               # unrecognised wording, no advice signals
            resp = render("unsure")
            resp.fields = {"examples": list(UI["examples"])}
            return resp, "rules"
        if r.bare_scheme:                                               # just a scheme name: ask which fact (no LLM)
            resp = render("clarify_field", scheme=r.schemes[0])
            resp.fields = {"scheme": r.schemes[0], "buttons": [(lbl, tpl.format(scheme=r.schemes[0])) for lbl, _f, tpl in FIELD_BUTTONS]}
            return resp, "rules"
        if i == "mixed":
            fa = factsqa.answer(r.field, r.schemes, r.asks_current) if r.field else None
            if fa and fa.text:
                return render("mixed", answer=fa.text, source_id=fa.source_id, page=fa.page), "facts"
            return render("advice"), "rules"                            # fact unavailable: decline the advice part alone
        if i == "factual" and r.field:
            fa = factsqa.answer(r.field, r.schemes, r.asks_current)
            if fa and fa.needs_scheme:
                resp = render("clarify")
                resp.fields = {"choices": list(SCHEME_NAMES), "field": r.field}
                return resp, "rules"
            if fa and fa.text:
                return render("fact", answer=fa.text, source_id=fa.source_id, page=fa.page), "facts"
        hit = answer_cache.lookup(text) if r.intent in ("concept", "howto") else None
        if hit:                                                         # vetted public answer: instant, no model call
            return render(hit["kind"], answer=hit["answer"], source_id=hit["source_id"], page=hit.get("page")), "curated"
        return self.rag_answer(text, r), "rag"                          # concepts, how-to, fields not in facts.json

    def rag_answer(self, text, r, budget=None):
        """Retrieval + Gemini within a hard budget (2 model attempts, 10 s). Budget exceeded -> LLMUnavailable -> service busy."""
        if self.llm is None:
            return render("service_unavailable")
        budget = budget or Budget(2, 10.0)
        q = expand_terms(prep(text))
        qvec = self.llm.embed_query(q, timeout=min(3.0, max(0.5, budget.left() - 4.0)))   # None -> BM25-only
        chunks = retrieve(q, r.schemes, r.intent, qvec)
        closest = (sources.first_of(doc_type="SID", scheme=r.schemes[0]) if r.schemes
                   else (chunks[0]["source_id"] if chunks else None))
        if not chunks:
            return render("not_found", closest_source_id=closest)
        why = None
        for _ in range(2):                                              # one validator-driven retry (counts toward the budget)
            status, answer, sid = generate(self.llm, q, chunks, why, budget)
            if status != "ok":
                break
            answer = strip_links(answer)
            ok, reasons = validate(answer, sid, chunks, r.schemes)
            if ok:
                pc = support_chunk(answer, sid, chunks)
                kind = r.intent if r.intent in ("concept", "howto") else "fact"
                return render(kind, answer=answer, source_id=sid, page=pc["page"] if pc else None)
            why = "; ".join(reasons)
            if budget.calls >= budget.max_calls:
                break
        return render("not_found", closest_source_id=closest)
