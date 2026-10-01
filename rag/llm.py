"""Thin Gemini wrapper with hard latency budgets.

  json(...)       answer generation: at most Budget.max_calls (2) model attempts and Budget.seconds (10 s) in total, with model
                  failover between attempts. Exceeding the budget raises LLMUnavailable -> the UI shows "service busy".
  json_once(...)  router classification: ONE attempt, 3 s, current model only, no failover. Failure -> caller uses the rules result.
  embed_query(..) query embedding: 3 s; on failure/timeout returns None (BM25-only retrieval) and pauses embedding for 60 s.

The SDK's own retry/backoff is disabled (attempts=1) so latency is bounded by us, not by hidden retries.
Privacy: exceptions carry only the exception *type*; nothing about prompts or responses is logged.
Free tier is ~20 generate requests/day/model, so every call site must be able to run without this module.
"""
import os
import time

import numpy as np

from rag.config import EMBED_DIM, EMBED_MODEL, mark_bad, resolve_model

_FAILOVER = ("503", "UNAVAILABLE", "500", "isconnected", "Timeout", "timed out", "ConnectError", "RemoteProtocol", "DeadlineExceeded",
             "429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND")
_embed_off_until = 0.0


class LLMUnavailable(Exception):
    """No Gemini model could serve the request in time (quota, overload, budget, missing key)."""


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


def _ttl(msg):
    """How long to skip a model after this error."""
    if "PerDay" in msg or "404" in msg or "NOT_FOUND" in msg:
        return 6 * 3600           # daily quota spent / retired
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return 60                 # per-minute limit
    return 120                    # overload / timeout


def _http(seconds):
    from google.genai import types
    return types.HttpOptions(timeout=max(500, int(seconds * 1000)), retry_options=types.HttpRetryOptions(attempts=1))


class LLM:
    def __init__(self, api_key=None):
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        self._client = None
        self.calls = 0          # generate attempts made (tests/eval assert zero for blocked inputs)
        self.embed_calls = 0

    @property
    def client(self):
        if self._client is None:
            if not self._key:
                raise LLMUnavailable("no API key")
            from google import genai
            self._client = genai.Client(api_key=self._key)
        return self._client

    def warm(self):
        """Resolve the model name once at startup (lists models; no generate request, so no quota is used)."""
        try:
            resolve_model(self.client)
        except Exception:
            pass

    def _generate(self, model, prompt, system, temperature, seconds):
        from google.genai import types
        cfg = types.GenerateContentConfig(temperature=temperature, response_mime_type="application/json",
                                          system_instruction=system, http_options=_http(seconds))
        self.calls += 1
        return self.client.models.generate_content(model=model, contents=prompt, config=cfg).text

    def json(self, prompt, system=None, temperature=0.1, budget=None):
        """JSON text from the model within `budget` (default: 2 attempts, 10 s). Fails over to the next model between attempts."""
        budget = budget or Budget()
        while True:
            seconds = budget.take()                       # raises LLMUnavailable when attempts/time are spent
            try:
                model = resolve_model(self.client)
            except LLMUnavailable:
                raise
            except Exception as e:
                raise LLMUnavailable(type(e).__name__) from None
            try:
                text = self._generate(model, prompt, system, temperature, seconds)
                if text:
                    return text
            except LLMUnavailable:
                raise
            except Exception as e:
                msg = str(e)
                if not any(k in msg for k in _FAILOVER):
                    raise LLMUnavailable(type(e).__name__) from None
                mark_bad(model, ttl=_ttl(msg))

    def json_once(self, prompt, timeout=3.0, temperature=0.0):
        """Classifier call: one attempt, `timeout` seconds, no failover. Any failure -> LLMUnavailable (caller uses rules)."""
        model = None
        try:
            model = resolve_model(self.client)
            text = self._generate(model, prompt, None, temperature, timeout)
            if text:
                return text
            raise LLMUnavailable("empty")
        except LLMUnavailable:
            raise
        except Exception as e:
            msg = str(e)
            if model and any(k in msg for k in _FAILOVER):
                mark_bad(model, ttl=_ttl(msg))
            raise LLMUnavailable(type(e).__name__) from None

    def embed_query(self, text, timeout=3.0):
        """Unit-length query embedding, or None (callers fall back to BM25 only). After a failure, skips embedding for 60 s."""
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
