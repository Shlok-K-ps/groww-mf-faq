"""Build vetted answers for common concept / how-to questions.

  python -m scripts.build_answer_cache            # generate candidates -> data/answer_cache.candidates.json (review these!)
  python -m scripts.build_answer_cache --only sip # (re)generate one entry
  python -m scripts.build_answer_cache --promote  # copy reviewed, validated candidates -> data/answer_cache.json

Each answer is produced by the real RAG path (retrieval + Gemini) and must pass rag.validate; nothing here ever sees user text.
Resumable: entries already in the candidates file are skipped, which matters on the free tier (~20 requests/day/model).
"""
import argparse
import json
import os
import sys
import time

from dotenv import load_dotenv

from rag import sources
from rag.config import DATA_DIR
from rag.llm import Budget, LLMUnavailable
from rag.pipeline import Assistant
from rag.router import rules
from rag.validate import count_sentences

CANDIDATES = os.path.join(DATA_DIR, "answer_cache.candidates.json")
FINAL = os.path.join(DATA_DIR, "answer_cache.json")

# id -> (kind, phrasings). The first phrasing is the one sent through retrieval + Gemini.
ENTRIES = {
    "ter": ("concept", ["What is TER?", "What is the total expense ratio?", "What is an expense ratio?", "Explain expense ratio"]),
    "exit_load": ("concept", ["What is exit load?", "What is an exit load?", "Explain exit load", "What is exit load in mutual funds?"]),
    "sip": ("concept", ["What is SIP?", "What is a SIP?", "What is a systematic investment plan?", "Explain SIP"]),
    "elss": ("concept", ["What is ELSS?", "What is an ELSS fund?", "Explain ELSS", "What is an ELSS tax saving fund?"]),
    "riskometer": ("concept", ["What is a riskometer?", "What is the riskometer?", "Explain riskometer", "What are the riskometer levels?"]),
    "benchmark": ("concept", ["What is a benchmark?", "What is a benchmark in mutual funds?", "Explain benchmark"]),
    "nav": ("concept", ["What is NAV?", "What is the net asset value?", "Explain NAV"]),
    "cas": ("concept", ["What is CAS?", "What is a consolidated account statement?", "Explain CAS"]),
    "lock_in": ("concept", ["What is a lock-in period?", "What is lock-in?", "Explain lock-in period"]),
    "amc": ("concept", ["What is an AMC?", "What is an asset management company?"]),
    "aum": ("concept", ["What is AUM?", "What are assets under management?"]),
    "capgains_statement": ("howto", ["How do I download my capital-gains statement?", "How to download capital gains statement",
                                     "How can I get my capital gain statement?", "Where do I get my capital gains statement?"]),
    "cas_howto": ("howto", ["How do I get a CAS?", "How to get a consolidated account statement", "How can I get my CAS?",
                            "Where do I download my CAS?"]),
}


def load(path):
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else []


def build(only=None):
    asst = Assistant()
    out = {e["id"]: e for e in load(CANDIDATES)}
    for eid, (kind, phr) in ENTRIES.items():
        if (only and eid != only) or (eid in out and out[eid].get("ok") and not only):
            continue
        r = rules(phr[0])
        rec = {"id": eid, "kind": kind, "questions": [], "answer": None, "source_id": None, "page": None, "ok": False}
        rec["questions"] = [q for q in phr if rules(q).intent == kind]          # keep only phrasings the router sends here
        rec["dropped_phrasings"] = [q for q in phr if q not in rec["questions"]]
        try:
            resp = asst.rag_answer(phr[0], r, Budget(3, 40))
            if resp.kind == kind:
                rec.update(answer=resp.text, source_id=resp.source_id, page=int(resp.source_url.split("#page=")[1])
                           if "#page=" in (resp.source_url or "") else None, ok=bool(rec["questions"]))
            else:
                rec["note"] = f"pipeline returned {resp.kind}"
        except LLMUnavailable as e:
            rec["note"] = f"LLM unavailable ({e})"
        if rec["source_id"]:
            rec["source_url"] = sources.source_url(rec["source_id"], rec["page"])
            rec["last_updated"] = sources.last_updated(rec["source_id"])
            rec["sentences"] = count_sentences(rec["answer"])
        out[eid] = rec
        json.dump(list(out.values()), open(CANDIDATES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[{'ok ' if rec['ok'] else 'FAIL'}] {eid}: {rec.get('note', '')}", flush=True)
        time.sleep(1)
    return list(out.values())


def promote():
    cands = [c for c in load(CANDIDATES) if c.get("ok")]
    keep = [{k: c[k] for k in ("id", "kind", "questions", "answer", "source_id", "page")} for c in cands]
    json.dump(keep, open(FINAL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"wrote {len(keep)} vetted answers to {FINAL}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--promote", action="store_true")
    a = ap.parse_args()
    load_dotenv(".env")
    sys.stdout.reconfigure(encoding="utf-8")
    promote() if a.promote else build(a.only)
