import os

import pytest

from rag import answer_cache


@pytest.fixture(autouse=True)
def no_curated_answers_by_default(monkeypatch):
    """Unit tests exercise the real model path; the curated cache is tested explicitly with its own temp file."""
    monkeypatch.setattr(answer_cache, "PATH", os.path.join("data", "__no_curated_cache__.json"))
    answer_cache._load.cache_clear()
    yield
    answer_cache._load.cache_clear()
