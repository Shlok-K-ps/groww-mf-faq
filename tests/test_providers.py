"""Provider order (Groq first, Gemini fallback), cooldowns, budget, classifier and zero-call guarantees - all with fake transports."""
import json
import time

import pytest
import requests

from rag import config, llm as llm_mod
from rag.llm import LLM, Budget, LLMUnavailable, ProviderError, groq_http
from rag.pipeline import Assistant

MODELS = {"data": [{"id": "openai/gpt-oss-120b", "active": True}, {"id": "qwen/qwen3.8-27b", "active": True},
                   {"id": "openai/gpt-oss-20b", "active": True}, {"id": "whisper-large-v3", "active": True}]}


def ok(answer="The riskometer is a SEBI risk scale.", sid="S21"):
    return {"choices": [{"message": {"content": json.dumps({"status": "ok", "answer": answer, "source_id": sid})}}]}


class FakeGroq:
    """Callable standing in for groq_http. `script` items: dict -> success body, Exception -> raised."""

    def __init__(self, script=(), models=MODELS):
        self.script, self.models, self.posts, self.gets, self.payloads = list(script), models, 0, 0, []

    def __call__(self, method, path, key, payload=None, timeout=10.0):
        if method == "GET":
            self.gets += 1
            if isinstance(self.models, Exception):
                raise self.models
            return self.models
        self.posts += 1
        self.payloads.append((payload, timeout))
        item = self.script.pop(0) if self.script else ok()
        if isinstance(item, Exception):
            raise item
        return item


class FakeGemini:
    def __init__(self, exc=None, text='{"status":"ok","answer":"Gemini answer.","source_id":"S21"}'):
        self.exc, self.text, self.n = exc, text, 0
        self.models = self

    def generate_content(self, **kw):
        self.n += 1
        if self.exc:
            raise self.exc
        return type("R", (), {"text": self.text})()

    def embed_content(self, **kw):
        raise RuntimeError("no embeddings in tests")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    config._bad.clear()
    llm_mod._groq_choice.clear()
    monkeypatch.setattr(llm_mod, "resolve_model", lambda c: "gemini-test")
    monkeypatch.setattr(llm_mod, "_embed_off_until", 0.0)
    yield
    config._bad.clear()
    llm_mod._groq_choice.clear()


def cool(*models):
    """Put Groq models on cooldown, as after a 429."""
    for m in models:
        config._bad["groq/" + m] = time.time() + 3600


ALL_BUT_120B = ("qwen/qwen3.8-27b", "openai/gpt-oss-20b")


def make(groq=None, gemini=None, groq_key="gk", gem_key="x"):
    l = LLM(api_key=gem_key, groq_key=groq_key, groq_request=groq or FakeGroq())
    l._client = gemini or FakeGemini()
    return l


# ----------------------------------------------------------------------------- model selection (checked once)
def test_models_are_chosen_from_the_live_list_once():
    g = FakeGroq()
    l = make(g)
    l.warm()
    l.json("p"), l.json("p")
    assert llm_mod._groq_choice == {"gen": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"], "cls": "qwen/qwen3.8-27b"}
    assert g.gets == 1                                                  # listed once, not per request


def test_prefers_llama_models_when_they_are_available():
    g = FakeGroq(models={"data": MODELS["data"] + [{"id": "llama-3.3-70b-versatile"}, {"id": "llama-3.1-8b-instant"}]})
    make(g).warm()
    assert llm_mod._groq_choice["gen"][:2] == ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]
    assert llm_mod._groq_choice["cls"] == "llama-3.1-8b-instant"


def test_list_failure_falls_back_to_known_json_models():
    make(FakeGroq(models=ProviderError("timeout"))).warm()
    assert llm_mod._groq_choice["gen"][0] in llm_mod.GROQ_GEN_PREF and llm_mod._groq_choice["cls"] in llm_mod.GROQ_CLS_PREF


# ----------------------------------------------------------------------------- order and fallback
def test_groq_is_tried_first_and_gemini_is_untouched():
    g, gem = FakeGroq(), FakeGemini()
    l = make(g, gem)
    assert json.loads(l.json("q", system="Return JSON only"))["status"] == "ok"
    assert (g.posts, gem.n) == (1, 0) and l.last == ("groq", "openai/gpt-oss-120b")
    payload, timeout = g.payloads[0]
    assert payload["response_format"] == {"type": "json_object"} and payload["messages"][0]["role"] == "system"
    assert payload["reasoning_effort"] == "low" and timeout <= 10


@pytest.mark.parametrize("failure", [ProviderError("http 429", 429, 60), ProviderError("timeout", None, 120),
                                     ProviderError("http 503", 503, 120)])
def test_groq_failure_falls_back_to_gemini_within_the_same_question(failure):
    cool(*ALL_BUT_120B)
    g, gem = FakeGroq([failure]), FakeGemini()
    l = make(g, gem)
    assert json.loads(l.json("q"))["answer"] == "Gemini answer."
    assert (g.posts, gem.n, l.calls) == (1, 1, 2) and l.last == ("gemini", "gemini-test")


def test_429_on_one_groq_model_tries_the_next_groq_model_before_gemini():
    g, gem = FakeGroq([ProviderError("http 429", 429, 15)]), FakeGemini()
    l = make(g, gem)
    assert json.loads(l.json("q"))["status"] == "ok"
    assert (g.posts, gem.n) == (2, 0) and l.last == ("groq", "qwen/qwen3.8-27b")
    assert g.payloads[0][0]["model"] == "openai/gpt-oss-120b" and g.payloads[1][0]["model"] == "qwen/qwen3.8-27b"


def test_429_puts_the_groq_model_on_cooldown_and_next_question_skips_it():
    cool(*ALL_BUT_120B)
    g, gem = FakeGroq([ProviderError("http 429", 429, 60)]), FakeGemini()
    l = make(g, gem)
    l.json("q1")
    assert g.posts == 1
    l.json("q2")
    assert g.posts == 1 and gem.n == 2                                  # no Groq call while cooling down
    config._bad["groq/openai/gpt-oss-120b"] = time.time() - 1           # cooldown over
    l.json("q3")
    assert g.posts == 2


def test_both_providers_failing_stops_at_two_attempts():
    cool(*ALL_BUT_120B)
    g, gem = FakeGroq([ProviderError("http 503", 503, 120)]), FakeGemini(exc=Exception("503 UNAVAILABLE"))
    l = make(g, gem)
    t0 = time.monotonic()
    with pytest.raises(LLMUnavailable):
        l.json("q")
    assert l.calls == 2 and g.posts == 1 and gem.n == 1 and time.monotonic() - t0 < 1


def test_no_groq_key_uses_gemini_only():
    g, gem = FakeGroq(), FakeGemini()
    l = make(g, gem, groq_key="")
    l.json("q")
    assert g.posts == 0 and g.gets == 0 and gem.n == 1 and l.last[0] == "gemini"


def test_no_keys_at_all_is_unavailable_not_a_crash():
    with pytest.raises(LLMUnavailable):
        LLM(api_key="", groq_key="", groq_request=FakeGroq()).json("q")


# ----------------------------------------------------------------------------- classifier
def test_classifier_uses_groq_small_model_one_attempt_no_gemini_fallback():
    g, gem = FakeGroq([ok()]), FakeGemini()
    l = make(g, gem)
    l.json_once("classify", timeout=3.0)
    payload, timeout = g.payloads[0]
    assert payload["model"] == "qwen/qwen3.8-27b" and timeout == 3.0 and all(m["role"] != "system" for m in payload["messages"])
    g.script = [ProviderError("http 429", 429, 60)]
    with pytest.raises(LLMUnavailable):
        l.json_once("classify again")
    assert g.posts == 2 and gem.n == 0                                  # no Gemini, no retry
    with pytest.raises(LLMUnavailable):
        l.json_once("and again")
    assert g.posts == 2                                                 # cooling down: no HTTP at all


def test_classifier_timeout_means_rules_result_takes_over():
    from rag.router import route
    l = make(FakeGroq([ProviderError("timeout", None, 120)]))
    r = route("mutual funds stuff", lambda p: l.json_once(p, 3.0))
    assert r.intent == "unsure" and r.via == "default"


# ----------------------------------------------------------------------------- HTTP layer (no leaks, 429 handling)
class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._b, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._b


def test_http_429_per_day_vs_per_minute(monkeypatch):
    monkeypatch.setattr(requests, "request", lambda *a, **k: Resp(429, {"error": {"message": "Rate limit reached ... tokens per day (TPD)"}}))
    with pytest.raises(ProviderError) as e:
        groq_http("POST", "/chat/completions", "gsk_secret", {})
    assert e.value.ttl >= 6 * 3600 and str(e.value) == "http 429"
    monkeypatch.setattr(requests, "request", lambda *a, **k: Resp(429, {"error": {"message": "per minute"}}, {"retry-after": "7"}))
    with pytest.raises(ProviderError) as e:
        groq_http("POST", "/chat/completions", "gsk_secret", {})
    assert e.value.ttl == 7


@pytest.mark.parametrize("exc,kind", [(requests.Timeout("Authorization: Bearer gsk_secret"), "timeout"),
                                      (requests.ConnectionError("Bearer gsk_secret prompt text"), "network")])
def test_http_errors_never_leak_key_or_prompt(monkeypatch, exc, kind):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(requests, "request", boom)
    with pytest.raises(ProviderError) as e:
        groq_http("POST", "/chat/completions", "gsk_secret", {"messages": [{"content": "my private question"}]})
    assert str(e.value) == kind and "gsk_secret" not in repr(e.value) and "private" not in repr(e.value)


def test_no_sdk_retries_single_request_per_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "request", lambda *a, **k: calls.append(1) or Resp(503))
    with pytest.raises(ProviderError):
        groq_http("POST", "/chat/completions", "k", {})
    assert len(calls) == 1


# ----------------------------------------------------------------------------- pipeline-level guarantees
@pytest.mark.parametrize("msg", [
    "What is the expense ratio of Groww Large Cap Fund (Direct)?", "What is the TER today?", "What is the exit load?",
    "Groww Small Cap", "Should I invest in Groww Small Cap?", "1-year return of Groww Large Cap?", "What's the weather?",
    "My PAN is ABCDE1234F, what's my SIP status?", "My OTP is 482913", "2345 6789 0123", "Kaunsa fund mere liye best hai?"])
def test_facts_pii_and_refusals_make_zero_provider_calls(msg):
    g, gem = FakeGroq(), FakeGemini()
    l = make(g, gem)
    Assistant(l).ask(msg)
    assert (g.posts, g.gets, gem.n, l.calls, l.embed_calls) == (0, 0, 0, 0, 0)


def test_rag_answer_comes_from_groq_with_validator_and_citation():
    g, gem = FakeGroq([ok("The riskometer is a standardised risk measurement scale introduced by SEBI for Mutual Funds.")]), FakeGemini()
    r = Assistant(make(g, gem)).ask("What is a riskometer?").response
    assert r.kind == "concept" and r.source_id == "S21" and (g.posts, gem.n) == (1, 0)


def test_validator_retry_goes_back_to_groq_and_counts_toward_the_budget():
    g = FakeGroq([ok("The riskometer has 99.9 levels."), ok("The riskometer is a SEBI risk scale for mutual funds.")])
    gem = FakeGemini()
    r = Assistant(make(g, gem)).ask("What is a riskometer?").response
    assert r.kind == "concept" and g.posts == 2 and gem.n == 0


def test_groq_429_during_rag_falls_back_to_gemini_answer():
    cool(*ALL_BUT_120B)
    g = FakeGroq([ProviderError("http 429", 429, 60)])
    gem = FakeGemini(text=json.dumps({"status": "ok", "answer": "The riskometer is a SEBI risk scale for mutual funds.",
                                      "source_id": "S21"}))
    r = Assistant(make(g, gem)).ask("What is a riskometer?").response
    assert r.kind == "concept" and (g.posts, gem.n) == (1, 1)


def test_all_providers_down_is_service_unavailable_fast():
    cool(*ALL_BUT_120B)
    g = FakeGroq([ProviderError("http 503", 503, 120)])
    gem = FakeGemini(exc=Exception("503 UNAVAILABLE"))
    t0 = time.monotonic()
    r = Assistant(make(g, gem)).ask("What is a riskometer?").response
    assert r.kind == "service_unavailable" and time.monotonic() - t0 < 2


def test_diagnose_reports_status_without_leaking_keys():
    l = make(FakeGroq(), FakeGemini(), groq_key="gsk_SECRETKEY123", gem_key="AIzaSECRET456")
    out = l.diagnose()
    text = json.dumps(out)
    assert out["GROQ_API_KEY set"] is True and "reachable" in out["Groq"] and "openai/gpt-oss-120b" in out["Groq generation chain"]
    assert "SECRET" not in text and "gsk_" not in text and "AIza" not in text
    bad = make(FakeGroq(models=ProviderError("http 401", 401, 3600)), FakeGemini(), groq_key="gsk_x")
    assert bad.diagnose()["Groq"] == "ERROR http 401"
    none = LLM(api_key="", groq_key="", groq_request=FakeGroq())
    assert none.diagnose() == {"GROQ_API_KEY set": False, "GEMINI_API_KEY set": False}


def test_provider_failures_are_logged_by_kind_only(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="gmf")
    l = make(FakeGroq([ProviderError("http 429", 429, 60)]), FakeGemini(), groq_key="gsk_x")
    l.json("my private question text")
    assert "provider_error provider=groq" in caplog.text and "http 429" in caplog.text
    assert "private" not in caplog.text and "gsk_" not in caplog.text
