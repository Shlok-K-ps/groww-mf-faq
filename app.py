"""Groww MF Facts Assistant - Streamlit UI.

Privacy: a message containing PII is never stored or echoed (the chat shows a placeholder), nothing user-typed is cached
across users, and st.cache_resource is used only to load the corpus. Only {intent, latency_ms, source_id, blocked} is logged.
"""
import html
import logging
import sys

import streamlit as st
from dotenv import load_dotenv

load_dotenv(".env")        # local dev only; on Render GEMINI_API_KEY comes from the environment
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)

from rag import sources                                     # noqa: E402
from rag.config import SCHEME_NAMES                         # noqa: E402
from rag.pipeline import Assistant, fill_scheme             # noqa: E402
from rag.retrieve import load_index                         # noqa: E402
from rag.pii import MAX_CHARS                               # noqa: E402
from rag.templates import HIDDEN_TOO_LONG, HIDDEN_USER_MESSAGE, UI   # noqa: E402

st.set_page_config(page_title=UI["title"], layout="centered")


@st.cache_resource(show_spinner=False)
def corpus_ready():
    """Corpus loading only (chunks + embeddings + BM25). Never user queries or answers."""
    load_index()
    from rag.llm import LLM
    LLM().warm()                # resolve the model name once; lists models only, no generate request
    return True


corpus_ready()

st.markdown("""
<style>
.chip{display:inline-block;padding:4px 12px;border-radius:999px;background:#E6FAF5;border:1px solid #00D09C;color:#0B6B57 !important;
      font-size:0.85rem;text-decoration:none !important;margin-top:4px}
.chip:hover{background:#CFF5EA}
.muted{color:#6B7B79;font-size:0.8rem;margin-top:4px}
.banner{border-left:4px solid #00D09C;background:#F4F7F6;padding:10px 14px;border-radius:8px;margin:8px 0 14px 0}
.refuse{border-left:3px solid #C9D4D2;padding-left:10px;margin-top:8px}
</style>""", unsafe_allow_html=True)

if "messages" not in st.session_state:
    st.session_state.messages = []      # {"role", "content" | "resp", "orig"}
    st.session_state.qcache = {}        # per-session only (identical normalised questions); lives in this browser session
    st.session_state.assistant = Assistant()

ss = st.session_state

st.title(UI["title"])
st.caption(UI["subtitle"])
st.markdown(f'<div class="banner"><b>{html.escape(UI["banner_head"])}</b> {html.escape(UI["banner_body"])}</div>',
            unsafe_allow_html=True)

with st.sidebar:
    st.subheader("Scope")
    st.write("**Groww Mutual Fund** (Groww Asset Management Ltd)")
    for s in SCHEME_NAMES:
        st.write(f"- {s}")
    st.caption("Direct Plan - Growth is assumed wherever values differ between plans.")
    st.subheader("Try asking")
    for k, q in enumerate(UI["examples"]):             # always available, even after the chat has started
        if st.button(q, key=f"side-ex-{k}", use_container_width=True):
            ss.pending = q
    with st.expander(f"Sources ({len(sources.load())})"):
        for sid, r in sources.load().items():
            st.markdown(f"- [{r['title']}]({r['url']}) · {r['publisher']} {r['doc_type']}")
    with st.expander("System status"):
        st.caption("Checks which AI providers are reachable. Lists models only; sends none of your questions.")
        if st.button("Check connections", key="diag"):
            for k, v in ss.assistant.llm.diagnose().items():
                st.write(f"**{k}:** {v}")
    with st.expander("About / limits"):
        st.write("Answers come only from official Groww MF, AMFI and SEBI documents and are as fresh as the last data build. "
                 "TER changes monthly. Coverage is 4 schemes, English only. If something isn't in my sources I'll say so.")


def show(resp):
    st.markdown(resp.text)
    if resp.kind == "mixed":
        st.markdown(f'<div class="refuse">{html.escape(resp.refusal_text)}</div>', unsafe_allow_html=True)   # plain text, no link
    if resp.kind == "advice":
        st.markdown(f'<a class="chip" href="{html.escape(resp.refusal_url)}" target="_blank">Learn more · SEBI Investor Website</a>',
                    unsafe_allow_html=True)
    if resp.source_url and resp.kind != "advice":
        label = "Closest source · " if resp.kind == "not_found" else ""
        st.markdown(f'<a class="chip" href="{html.escape(resp.source_url)}" target="_blank">{label}{html.escape(resp.source_label)}</a>',
                    unsafe_allow_html=True)
        if resp.last_updated and resp.kind != "not_found":
            st.markdown(f'<div class="muted">Last updated from sources: {resp.last_updated}</div>', unsafe_allow_html=True)


if not ss.messages:
    st.info(UI["welcome"])
    cols = st.columns(len(UI["examples"]))
    for c, q in zip(cols, UI["examples"]):
        if c.button(q, use_container_width=True, key=f"ex-{q}"):
            ss.pending = q

for i, m in enumerate(ss.messages):
    with st.chat_message(m["role"]):
        if m["role"] == "user":
            # plain text only: escaped HTML, so no markdown, no auto-linked URLs in what the user typed
            st.html(f'<div style="white-space:pre-wrap;word-break:break-word">{html.escape(m["content"])}</div>')
        else:
            show(m["resp"])
            last = i == len(ss.messages) - 1
            if m["resp"].kind == "clarify_field" and last:
                btns = m["resp"].fields["buttons"]
                for row in (btns[:3], btns[3:]):
                    for c, (lbl, q) in zip(st.columns(3), row):
                        if c.button(lbl, key=f"fld-{i}-{lbl}", use_container_width=True):
                            ss.pending = q
            if m["resp"].kind == "unsure" and last:
                for k, q in enumerate(m["resp"].fields["examples"]):
                    if st.button(q, key=f"uex-{i}-{k}", use_container_width=True):
                        ss.pending = q
            if m["resp"].kind == "clarify" and last and m.get("orig"):
                cols = st.columns(len(SCHEME_NAMES))
                for c, s in zip(cols, SCHEME_NAMES):
                    if c.button(s.replace("Groww ", "").replace(" Fund", ""), key=f"sch-{i}-{s}", use_container_width=True):
                        ss.pending = fill_scheme(m["orig"], s)

typed = st.chat_input("Ask a factual question about the 4 schemes", max_chars=MAX_CHARS)
question = typed or ss.pop("pending", None)
if question:
    with st.spinner("Checking official sources..."):
        res = ss.assistant.ask(question, ss.qcache)
    if res.blocked:     # never store or echo a blocked message; ONLY this message is replaced - earlier turns stay as they are
        hidden = HIDDEN_TOO_LONG if res.response.kind == "too_long" else HIDDEN_USER_MESSAGE
        ss.messages.append({"role": "user", "content": hidden})
        ss.messages.append({"role": "assistant", "resp": res.response})
    else:
        ss.messages.append({"role": "user", "content": question})
        ss.messages.append({"role": "assistant", "resp": res.response, "orig": question})
    question = None
    st.rerun()

st.markdown(f'<div class="muted" style="margin-top:2rem">{html.escape(UI["footer"])}</div>', unsafe_allow_html=True)
