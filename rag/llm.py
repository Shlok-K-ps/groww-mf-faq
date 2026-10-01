"""LLM access with two generation providers and hard latency budgets.

  Generation (json):  Groq first (JSON-mode models from Groq's live models list, best first; a 429 on one tries the next, because the
                      free tier limits tokens/minute PER MODEL), then Gemini Flash as the fallback. At most Budget.max_calls (2)
                      attempts and Budget.seconds (10 s) in total, shared by failover and the validator retry. Exceeding the budget
                      raises LLMUnavailable -> the UI shows "service busy".
  Classification (json_once): Groq small/fast model, ONE attempt, 3 s, no failover. Failure -> the caller uses the rules result.
  Query embedding:    Gemini gemini-embedding-001 only (the index was built with it); 3 s, failure -> BM25-only retrieval.

No provider SDK retries/backoff anywhere (Groq goes through plain HTTPS with one attempt; Gemini retry_options attempts=1), so latency
is bounded by us. A 429 (or any provider error) puts that model on a cooldown (daily-limit 429s: hours; per-minute: seconds).
Privacy: errors carry only a short kind/status - never keys, prompts or responses - and nothing here logs request content.
"""
import os
import time

import numpy as np
import requests

from rag.config import EMBED_DIM, EMBED_MODEL, mark_bad, resolve_model

GROQ_URL = "https://api.groq.com/openai/v1"
# Preference order. Every model present in Groq's live models list is used, in this order, as the Groq generation chain
# (all verified to support JSON mode). Models that Groq has retired simply don't appear in the list and are skipped.
GROQ_GEN_PREF = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
GROQ_CLS_PREF = ["llama-3.1-8b-instant", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
_groq_choice = {}          # {"gen": [models], "cls": model}, chosen once per process

_GEMINI_FAILOVER = ("503", "UNAVAILABLE", "500", "isconnected", "Timeout", "timed out", "ConnectError", "RemoteProtocol",
                    "DeadlineExceeded", "429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND")
_embed_off_until = 0.0


class LLMUnavailable(Exception):
    """No provider could serve the request in time (quota, overload, budget, missing keys)."""


class ProviderError(Exception):
    """A provider call failed. `ttl` = seconds to skip that model afterwards. Message holds only a short kind, never content."""

    def __init__(self, kind, status=None, ttl=120):
        super().__init__(kind)
        self.status, self.ttl = status, ttl


class Budget:
    """Per-question limit on model attempts and wall-clock time."""

    def __init__(self, max_calls=2, seconds=10.0):
        self.max_calls, self.calls = max_calls, 0
        self.deadline = time.monotonic() + seconds

    def left(self):
        return self.deadline - time.monotonic()

    def take(self):
        """Reserve one attempt; returns the seconds this attempt may use. Raises when the budget is spent."""
        if self.calls >= self.max_calls or self.left() < 0.5:
            raise LLMUnavailable("budget")
        self.calls += 1
        return self.left()


def _gemini_ttl(msg):
    if "PerDay" in msg or "404" in msg or "NOT_FOUND" in msg:
        return 6 * 3600
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return 60
    return 120


def _http(seconds):
    from google.genai import types
    return types.HttpOptions(timeout=max(500, int(seconds * 1000)), retry_options=types.HttpRetryOptions(attempts=1))


def _is_bad(key):
    from rag.config import _bad
    return _bad.get(key, 0) > time.time()


def groq_http(method, path, key, payload=None, timeout=10.0):
    """One HTTPS request to Groq (OpenAI-compatible API). No retries. Raises ProviderError(kind, status, ttl); never leaks content."""
    try:
        r = requests.request(method, GROQ_URL + path, json=payload, timeout=(min(3.0, timeout), max(0.5, timeout)),
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except requests.Timeout:
        raise ProviderError("timeout", None, 120) from None
    except requests.RequestException:
        raise ProviderError("network", None, 120) from None
    if r.status_code == 200:
        return r.json()
    msg = ""
    try:
        msg = (r.json().get("error") or {}).get("message", "")
    except Exception:
        pass
    low = msg.lower()
    if r.status_code == 429:
        if "per day" in low or "(tpd)" in low or "(rpd)" in low:
            ttl = 6 * 3600                                    # daily quota spent
        else:
            try:
                ttl = min(300, max(5, int(float(r.headers.get("retry-after", 60)))))
            except ValueError:
                ttl = 60
    elif r.status_code in (400, 401, 403, 404):
        ttl = 6 * 3600                                        # bad key / model gone / JSON mode unsupported
    else:
        ttl = 120
    raise ProviderError(f"http {r.status_code}", r.status_code, ttl)


class LLM:
    def __init__(self, api_key=None, groq_key=None, groq_request=None):
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        self._groq_key = os.environ.get("GROQ_API_KEY") if groq_key is None else groq_key     # "" disables Groq (tests)
        self._groq = groq_request or groq_http
        self._client = None
        self.calls = 0           # generate attempts across providers (tests/eval assert zero for blocked inputs)
        self.groq_calls = 0
        self.embed_calls = 0
        self.last = None         # (provider, model) that produced the last answer; for the CLI/eval only, never logged

    # ------------------------------------------------------------------ Gemini
    @property
    def client(self):
        if self._client is None:
            if not self._key:
                raise LLMUnavailable("no API key")
            from google import genai
            self._client = genai.Client(api_key=self._key)
        return self._client

    def _gemini_generate(self, model, prompt, system, temperature, seconds):
        from google.genai import types
        cfg = types.GenerateContentConfig(temperature=temperature, response_mime_type="application/json",
                                          system_instruction=system, http_options=_http(seconds))
        self.calls += 1
        return self.client.models.generate_content(model=model, contents=prompt, config=cfg).text

    # ------------------------------------------------------------------ Groq
    def _groq_models(self):
        """Pick the generation chain + classifier model once per process from Groq's live models list."""
        if _groq_choice:
            return _groq_choice
        try:
            data = self._groq("GET", "/models", self._groq_key, None, 5.0)
            have = {m["id"] for m in data.get("data", []) if m.get("active", True)}
        except Exception:
            have = set()
        gen = [m for m in GROQ_GEN_PREF if m in have] or ([] if have else GROQ_GEN_PREF[1:])     # list unreachable: known JSON models
        cls = next((m for m in GROQ_CLS_PREF if m in have), None) or (GROQ_CLS_PREF[1] if not have else None)
        _groq_choice.update(gen=gen, cls=cls or (gen[0] if gen else None))     # empty -> Groq skipped without re-listing every call
        return _groq_choice

    def _groq_generate(self, model, prompt, system, temperature, seconds):
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload = {"model": model, "messages": msgs, "temperature": temperature, "max_completion_tokens": 500,
                   "response_format": {"type": "json_object"}}
        if model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"               # reasoning models: keep latency low
        self.calls += 1
        self.groq_calls += 1
        data = self._groq("POST", "/chat/completions", self._groq_key, payload, seconds)
        return data["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------ public
    def warm(self):
        """Startup: choose the Groq models from the live list and resolve the Gemini model name. Lists models only (no generate quota)."""
        if self._groq_key:
            self._groq_models()
        if self._key:
            try:
                resolve_model(self.client)
            except Exception:
                pass

    def _pick(self):
        """Next provider to try: the first Groq generation model not cooling down (each has its own rate limit), else Gemini."""
        if self._groq_key:
            for m in self._groq_models().get("gen") or []:
                if not _is_bad("groq/" + m):
                    return "groq", m
        if self._key:
            try:
                return "gemini", resolve_model(self.client)
            except Exception:
                pass
        raise LLMUnavailable("no provider available")

    def json(self, prompt, system=None, temperature=0.1, budget=None):
        """JSON text within `budget` (default 2 attempts, 10 s). Groq first, then Gemini; a failed model is skipped for its cooldown."""
        budget = budget or Budget()
        while True:
            seconds = budget.take()                           # raises LLMUnavailable when attempts/time are spent
            prov, model = self._pick()
            try:
                gen = self._groq_generate if prov == "groq" else self._gemini_generate
                text = gen(model, prompt, system, temperature, seconds)
                if text:
                    self.last = (prov, model)
                    return text
                ttl = 120                                     # empty answer: treat like a transient failure
            except ProviderError as e:
                ttl = e.ttl
            except LLMUnavailable:
                raise
            except Exception as e:
                msg = str(e)
                if prov == "groq" or not any(k in msg for k in _GEMINI_FAILOVER):
                    raise LLMUnavailable(type(e).__name__) from None
                ttl = _gemini_ttl(msg)
            mark_bad(("groq/" if prov == "groq" else "") + model, ttl=ttl)

    def json_once(self, prompt, timeout=3.0, temperature=0.0):
        """Classifier: Groq small/fast model, ONE attempt, `timeout` s, no failover (Gemini only if no Groq key is configured).
        Any failure -> LLMUnavailable and the caller uses the rules result."""
        if self._groq_key:
            model = self._groq_models().get("cls")
            if not model or _is_bad("groq/" + model):
                raise LLMUnavailable("classifier cooling down")
            try:
                text = self._groq_generate(model, prompt, None, temperature, timeout)
                if text:
                    self.last = ("groq", model)
                    return text
                raise ProviderError("empty")
            except ProviderError as e:
                mark_bad("groq/" + model, ttl=e.ttl)
                raise LLMUnavailable(str(e)) from None
            except Exception as e:
                raise LLMUnavailable(type(e).__name__) from None
        model = None
        try:
            model = resolve_model(self.client)
            text = self._gemini_generate(model, prompt, None, temperature, timeout)
            if text:
                self.last = ("gemini", model)
                return text
            raise LLMUnavailable("empty")
        except LLMUnavailable:
            raise
        except Exception as e:
            msg = str(e)
            if model and any(k in msg for k in _GEMINI_FAILOVER):
                mark_bad(model, ttl=_gemini_ttl(msg))
            raise LLMUnavailable(type(e).__name__) from None

    def embed_query(self, text, timeout=3.0):
        """Unit-length query embedding from gemini-embedding-001 (what the index was built with), or None -> BM25-only retrieval.
        After a failure, skips embedding for 60 s."""
        global _embed_off_until
        if time.monotonic() < _embed_off_until:
            return None
        try:
            from google.genai import types
            self.embed_calls += 1
            r = self.client.models.embed_content(
                model=EMBED_MODEL, contents=[text],
                config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=EMBED_DIM,
                                                http_options=_http(timeout)))
            v = np.array(r.embeddings[0].values, dtype=np.float32)
            return v / np.linalg.norm(v)
        except Exception:
            _embed_off_until = time.monotonic() + 60
            return None
