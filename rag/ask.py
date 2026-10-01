"""CLI:  python -m rag.ask "What is the lock-in period for Groww ELSS Tax Saver Fund?"   [--offline]

The question is never echoed back or logged; only the answer and an anonymous meta line are printed."""
import argparse
import logging
import sys

from dotenv import load_dotenv

from rag.pipeline import Assistant
from rag.templates import to_text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="+")
    ap.add_argument("--offline", action="store_true", help="rules + facts templates only; no Gemini calls")
    a = ap.parse_args()
    load_dotenv(".env")
    logging.basicConfig(level=logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8")
    asst = Assistant(use_llm=not a.offline)
    res = asst.ask(" ".join(a.question))
    print(to_text(res.response))
    if res.response.kind == "clarify":
        print("[buttons] " + " | ".join(res.response.fields["choices"]))
    calls = (asst.llm.calls + asst.llm.embed_calls) if asst.llm else 0
    last = getattr(asst.llm, "last", None) if asst.llm else None
    prov = f" provider={last[0]}/{last[1]}" if last else ""
    print(f"\n[intent={res.intent} kind={res.response.kind} via={res.via} latency={res.latency_ms}ms llm_calls={calls}{prov}]")


if __name__ == "__main__":
    main()
