import json
import time

import pytest

from rag import answer_cache, llm as llm_mod
from rag.llm import LLM, Budget, LLMUnavailable
from rag.pipeline import Assistant
from rag.router import route
from rag.templates import UI, to_text


class FakeModels:
    def __init__(self, exc=None, text='{"status":"ok"}'):
        self.exc, self.text, self.n = exc, text, 0

    def generate_content(self, **kw):
        self.n += 1
        if self.exc:
            raise self.exc
        return type("R", (), {"text": self.text})()

    def embed_content(self, **kw):
        self.n += 1
        raise RuntimeError("boom")


class FakeClient:
    def __init__(self, models):
        self.models = models


@pytest.fixture
def real_llm(monkeypatch):
    """A real LLM object wired to a fake client, so the budget logic itself is exercised (no network, no quota)."""
    monkeypatch.setattr(llm_mod, "resolve_model", lambda c: "m1")
    monkeypatch.setattr(llm_mod, "mark_bad", lambda *a, **k: None)
    monkeypatch.setattr(llm_mod, "_embed_off_until", 0.0)

    def make(models):
        l = LLM(api_key="x")
        l._client = FakeClient(models)
        return l
    return make


def test_budget_allows_two_attempts_then_stops():
    b = Budget(2, 10)
    b.take(); b.take()
    with pytest.raises(LLMUnavailable):
        b.take()


def test_budget_stops_when_time_is_spent(monkeypatch):
    b = Budget(2, 0.4)
    with pytest.raises(LLMUnavailable):
        b.take()                                   # < 0.5 s left


def test_generation_never_exceeds_two_attempts(real_llm):
    m = FakeModels(exc=Exception("503 UNAVAILABLE"))
    l = real_llm(m)
    t0 = time.monotonic()
    with pytest.raises(LLMUnavailable):
        l.json("q")
    assert m.n == 2 and l.calls == 2 and time.monotonic() - t0 < 1     # no sleeps, no hidden retries


def test_unknown_error_type_is_not_leaked_or_retried(real_llm):
    m = FakeModels(exc=ValueError("secret prompt text"))
    with pytest.raises(LLMUnavailable) as e:
        real_llm(m).json("q")
    assert "secret" not in str(e.value) and m.n == 1


def test_classifier_is_one_attempt_no_failover(real_llm):
    m = FakeModels(exc=Exception("429 RESOURCE_EXHAUSTED"))
    l = real_llm(m)
    with pytest.raises(LLMUnavailable):
        l.json_once("classify this", timeout=3)
    assert m.n == 1


def test_embedding_failure_falls_back_and_pauses(real_llm):
    m = FakeModels()
    l = real_llm(m)
    assert l.embed_query("what is ter") is None and m.n == 1
    assert l.embed_query("what is sip") is None and m.n == 1          # breaker: no second attempt within 60 s
    llm_mod._embed_off_until = 0.0


def test_unsure_and_classifier_down_means_neutral_not_refusal():
    def down(prompt):
        raise LLMUnavailable("down")
    r = route("mutual funds stuff", down)                             # MF word, no question, no advice signal
    assert r.intent == "unsure" and r.via == "default"
    assert route("mutual funds stuff", None).intent == "unsure"       # offline: same


def test_pipeline_unsure_wording_gets_neutral_message_and_example_buttons():
    class Down:
        calls = embed_calls = 0

        def json_once(self, *a, **k):
            raise LLMUnavailable("down")

        def json(self, *a, **k):
            raise AssertionError("generate must not be called")

        def embed_query(self, *a, **k):
            raise AssertionError("embedding must not be called")
    r = Assistant(Down()).ask("mutual funds stuff").response
    assert r.kind == "unsure" and "I'm not sure I understood" in r.text and "Refus" not in r.text
    assert r.fields["examples"] == UI["examples"] and "can't advise" not in r.text and "SEBI" not in r.text


def test_rag_outage_is_service_unavailable_fast():
    class Down:
        calls = embed_calls = 0

        def json(self, *a, **k):
            raise LLMUnavailable("budget")

        def embed_query(self, *a, **k):
            return None
    t0 = time.monotonic()
    r = Assistant(Down()).ask("What is a riskometer?")
    assert r.response.kind == "service_unavailable" and time.monotonic() - t0 < 1


# ----------------------------------------------------------------------------- curated answer cache
@pytest.fixture
def curated(tmp_path, monkeypatch):
    f = tmp_path / "answer_cache.json"
    f.write_text(json.dumps([{"id": "ter", "kind": "concept", "source_id": "S16", "page": None,
                              "questions": ["what is ter", "what is the total expense ratio", "explain expense ratio"],
                              "answer": "TER is the expense charged to a scheme as a percentage of its corpus."}]), encoding="utf-8")
    monkeypatch.setattr(answer_cache, "PATH", str(f))
    answer_cache._load.cache_clear()
    yield f
    answer_cache._load.cache_clear()


def test_curated_exact_and_near_exact_match(curated):
    assert answer_cache.lookup("What is TER?", str(curated))["id"] == "ter"
    assert answer_cache.lookup("what is  ter ?", str(curated))["id"] == "ter"
    assert answer_cache.lookup("what is the exit load", str(curated)) is None
    assert answer_cache.lookup("what is a riskometer", str(curated)) is None


def test_curated_hit_serves_instantly_with_zero_model_calls(curated):
    class Never:
        calls = embed_calls = 0

        def json(self, *a, **k):
            raise AssertionError("no model call expected")

        def json_once(self, *a, **k):
            raise AssertionError("no model call expected")

        def embed_query(self, *a, **k):
            raise AssertionError("no embedding call expected")
    res = Assistant(Never()).ask("What is TER?")
    assert res.via == "curated" and res.response.kind == "concept"
    out = to_text(res.response)
    assert "Source: https://www.mutualfundssahihai.com/" in out and "Last updated from sources:" in out


def test_curated_cache_file_never_receives_user_text(curated):
    before = curated.read_text(encoding="utf-8")
    Assistant(type("N", (), {"calls": 0, "embed_calls": 0, "json": lambda *a, **k: (_ for _ in ()).throw(LLMUnavailable("x")),
                             "embed_query": lambda *a, **k: None})()).ask("What is the weather in Pune?")
    assert curated.read_text(encoding="utf-8") == before
