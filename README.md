# Groww MF Facts Assistant

A facts-only assistant that answers questions about four Groww Mutual Fund schemes in three sentences or fewer, always with one
official source link and a "last updated" date. It refuses investment advice, never computes returns, and blocks personal data
before anything else runs. **Student prototype - not affiliated with Groww. Facts-only, no investment advice.**

> This README is being built up phase by phase. Sections still to come: eval results by category, sample Q&A, full limits list.

## Scope

Groww Mutual Fund (Groww Asset Management Ltd): **Groww Large Cap Fund, Groww Multicap Fund, Groww ELSS Tax Saver Fund, Groww Small
Cap Fund**. Groww MF has no flexi-cap scheme, so Multicap (the closest diversified-equity scheme) stands in for it. Direct Plan -
Growth is assumed wherever values differ between plans. Sources are official Groww MF, AMFI and SEBI pages only (`data/sources.csv`).

## How an answer is produced

```
message -> PII guard (regex, no network) -> route (rules first; Groq classifier only if unsure, 3 s)
        -> facts.json code templates (expense ratio, riskometer, exit load, min SIP/lumpsum, lock-in, benchmark, managers)  [no LLM]
        -> or curated answers / retrieval (BM25 + Gemini embeddings) + LLM -> validator -> render
```

Code owns the format: the model only writes one answer sentence block; the source URL (with `#page=N` for PDFs) and the date come
from `sources.csv`. Facts (TER, riskometer, exit load, ...) are answered from `data/facts.json` by code templates with no model call.

## LLM providers: Groq first, Gemini as fallback

| Job | Provider and model | Why |
|---|---|---|
| Answer generation (concepts, how-to, fields not in `facts.json`) | **Groq** `openai/gpt-oss-120b` (JSON mode), then the next Groq model on a 429, then **Gemini Flash** | Groq answers in about 1-2 s; Gemini's free tier allows only about 20 requests/day per model and is often overloaded, which made answers take 20-30 s or fail. |
| Intent classifier (only when the rules are unsure) | **Groq** `qwen/qwen3.8-27b`, one attempt, 3 s cap, no failover; on failure the rules result is used | A small fast model; no failover chain keeps the worst case at 3 s. |
| Query embeddings | **Gemini** `gemini-embedding-001`, 3 s cap, BM25-only retrieval if it fails | The corpus index was built with this model, so queries must use it; switching would mean re-embedding everything. |

- The Groq models are picked once at startup from Groq's live models list (JSON-mode models, best first), so a retired model is skipped
  instead of breaking the app. As of this build `llama-3.3-70b-versatile` is no longer offered, so `gpt-oss-120b` leads.
- **Budget per question:** at most 2 model attempts and 10 s in total (shared by provider failover and the validator's single retry).
  Beyond that the user sees "Service busy". The SDKs' own retry/backoff is disabled, so latency is bounded by our code.
- **Rate limits:** a 429 puts that model on a cooldown (Retry-After seconds for per-minute limits, hours for daily limits). Groq's free
  tier limits tokens per minute per model (8,000 for `gpt-oss-120b`), so the context sent to the model is capped at about 1,500 tokens
  and a second Groq model takes over when the first is limited.
- **Same prompts and validator for both providers.** The validator checks the citation was retrieved, at most 3 sentences, no advice
  words, no URLs, and that every number appears in the cited source.
- **Privacy:** only a question that has already passed the PII guard, plus public document text, is ever sent to Groq or Gemini.
  Messages containing personal data make no network call at all. Nothing user-typed is logged, only
  `{intent, latency_ms, source_id, blocked}`.
- Typical warm latency on the live path (embedding + retrieval + generation + validation): about 2 s. Facts, refusals and PII blocks
  take milliseconds because they never call a model.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys below
python ingest.py parse && python ingest.py embed && python ingest.py facts && python ingest.py patch   # rebuild data/ (optional; data/ is committed)
streamlit run app.py
```

| Variable | Used for |
|---|---|
| `GROQ_API_KEY` | answer generation and the intent classifier (primary) |
| `GEMINI_API_KEY` | generation fallback, query embeddings, and `ingest.py` |

Both are read from the environment (never committed; `.env` is git-ignored). Either key alone works: without Groq the app uses
Gemini only; without Gemini it loses embeddings (BM25-only retrieval) and the fallback.

Deploy: Render free web service from `render.yaml` (set `GROQ_API_KEY` and `GEMINI_API_KEY` as secrets). `data/` is committed, so the app
only loads files at start-up; there is no database.

Try it from the terminal: `python -m rag.ask "What is the lock-in period for Groww ELSS Tax Saver Fund?"` (add `--offline` for rules and
templates only, zero model calls).
