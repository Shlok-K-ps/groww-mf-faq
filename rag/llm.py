"""Thin Gemini wrapper: model failover with expiring blacklist, retry on transient errors, query embeddings.

Privacy: exceptions raised here carry only the exception *type*; callers log nothing about the prompt or the response.
Free tier is ~20 generate requests/day/model, so every call site must be able to run without this module (templates, rules).
"""
import os
import time

import numpy as np

from rag.config import EMBED_DIM, EMBED_MODEL, mark_bad, resolve_model

_RETRY_NOW = ("503", "UNAVAILABLE", "500", "isconnected", "Timeout", "timed out", "ConnectError", "RemoteProtocol")
_FAILOVER = _RETRY_NOW + ("429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND")


class LLMUnavailable(Exception):
    """No Gemini model could serve the request (quota, overload, missing key)."""


class LLM:
    def __init__(self, api_key=None):
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        self._client = None
        self.calls = 0      # generate calls made (used by tests / eval to assert zero calls on blocked inputs)
        self.embed_calls = 0

    @property
    def client(self):
        if self._client is None:
            if not self._key:
                raise LLMUnavailable("no API key")
            from google import genai
            self._client = genai.Client(api_key=self._key)
        return self._client

    def json(self, prompt, system=None, temperature=0.1, models_to_try=6):
        """Returns the model's JSON text. Fails over across Flash models; raises LLMUnavailable if none works."""
        from google.genai import types
        cfg = types.GenerateContentConfig(temperature=temperature, response_mime_type="application/json",
                                          system_instruction=system)
        for _ in range(models_to_try):
            try:
                model = resolve_model(self.client)
            except LLMUnavailable:
                raise
            except Exception as e:
                raise LLMUnavailable(type(e).__name__) from None
            marked = False
            for attempt in range(2):
                try:
                    self.calls += 1
                    r = self.client.models.generate_content(model=model, contents=prompt, config=cfg)
                    if r.text:
                        return r.text
                    break
                except Exception as e:
                    msg = str(e)
                    if not any(k in msg for k in _FAILOVER):
                        raise LLMUnavailable(type(e).__name__) from None
                    if "PerDay" in msg:                       # daily quota spent: skip this model for hours, not minutes
                        mark_bad(model, ttl=6 * 3600)
                        marked = True
                        break
                    if attempt == 0 and any(k in msg for k in _RETRY_NOW) and "429" not in msg:
                        time.sleep(2)
                        continue
                    break
            if not marked:
                mark_bad(model)
        raise LLMUnavailable("all models failed")

    def embed_query(self, text):
        """Unit-length query embedding, or None if embeddings are unavailable (callers fall back to BM25 only)."""
        try:
            from google.genai import types
            self.embed_calls += 1
            r = self.client.models.embed_content(
                model=EMBED_MODEL, contents=[text],
                config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=EMBED_DIM))
            v = np.array(r.embeddings[0].values, dtype=np.float32)
            return v / np.linalg.norm(v)
        except Exception:
            return None
