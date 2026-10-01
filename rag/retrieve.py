"""Hybrid retrieval: 0.5 * BM25 + 0.5 * cosine (both min-max normalised), top-k = 6. Scheme-filtered.

The corpus (442 chunks) lives in memory. load_index() is the only cached thing and it holds corpus data, never user input.
"""
import json
import os
import re
from functools import lru_cache

import numpy as np
from rank_bm25 import BM25Okapi

from rag.config import DATA_DIR

TOP_K = 6
GENERAL_TYPES = {"Education", "FAQ", "Charter"}          # concept / how-to questions are answered from these
SCHEME_TYPES = {"KIM", "SID", "Factsheet", "TER", "Riskometer"}


EXPANSIONS = {
    "ter": "total expense ratio expense ratio expenses charged", "sip": "systematic investment plan", "elss": "equity linked savings scheme tax saving",
    "nav": "net asset value", "cas": "consolidated account statement", "amc": "asset management company", "aum": "assets under management",
    "cagr": "compound annual growth rate", "idcw": "income distribution cum capital withdrawal dividend", "kim": "key information memorandum",
    "sid": "scheme information document", "stp": "systematic transfer plan", "swp": "systematic withdrawal plan",
}


def expand_terms(q):
    """Append the long form of known abbreviations (the AMFI pages often use the long form only)."""
    extra = [v for k, v in EXPANSIONS.items() if re.search(r"(?<![a-z0-9])" + k + r"(?![a-z0-9])", q.lower())]
    return q + (" (" + "; ".join(extra) + ")" if extra else "")


def tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower())


@lru_cache(maxsize=1)
def load_index():
    chunks = json.load(open(os.path.join(DATA_DIR, "chunks.json"), encoding="utf-8"))
    emb = np.load(os.path.join(DATA_DIR, "index", "embeddings.npy"))
    ids = json.load(open(os.path.join(DATA_DIR, "index", "chunk_ids.json")))
    assert ids == [c["chunk_id"] for c in chunks], "index out of sync with chunks.json; rerun python ingest.py embed"
    bm25 = BM25Okapi([tokenize(c["text"]) for c in chunks])
    return chunks, emb, bm25


def _norm(x):
    x = np.asarray(x, dtype=np.float64)
    span = x.max() - x.min() if len(x) else 0
    return (x - x.min()) / span if span > 0 else np.zeros_like(x)


def candidates(chunks, schemes, intent):
    """Indexes eligible for this question: scheme filter (+ALL chunks) and doc-type preference by intent."""
    idx = []
    for i, c in enumerate(chunks):
        if schemes and c["scheme"] not in schemes and c["scheme"] != "ALL":
            continue
        if intent in ("concept", "howto") and not schemes and c["doc_type"] not in GENERAL_TYPES:
            continue
        idx.append(i)
    return idx


def retrieve(query, schemes=(), intent="factual", query_vec=None, k=TOP_K):
    """Returns the top-k chunk dicts (best first). `query_vec` None -> BM25 only."""
    chunks, emb, bm25 = load_index()
    idx = candidates(chunks, set(schemes), intent)
    if not idx:
        return []
    sub = np.array(idx)
    bm = _norm(bm25.get_scores(tokenize(query))[sub])
    if query_vec is not None:
        score = 0.5 * bm + 0.5 * _norm(emb[sub] @ query_vec)
    else:
        score = bm
    order = np.argsort(-score, kind="stable")[:k]
    return [dict(chunks[sub[i]], score=float(score[i])) for i in order if score[i] > 0]
