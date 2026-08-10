"""Fixtures and fakes.

Nothing in the test suite touches a network. The two seams that would are the
Anthropic client and the DataForSEO client, and both are replaced here.
"""

from __future__ import annotations

import pytest

from app import create_app
from app.clients.dataforseo import KeywordMetrics
from app.clients.llm import LLMResponse
from app.extensions import db as _db


class FakeLLM:
    """Returns queued responses in order; records what it was asked.

    Queue an Exception instance to make the next call raise - that is how the
    partial-failure tests inject a mid-batch failure.
    """

    def __init__(self, responses: list[str | Exception] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def queue(self, *responses: str | Exception) -> None:
        self.responses.extend(responses)

    def complete(self, *, model, system, user, max_tokens=2000, temperature=0.4, prefill=None):
        self.calls.append(
            {"model": model, "system": system, "user": user, "prefill": prefill}
        )
        if not self.responses:
            raise AssertionError("FakeLLM ran out of queued responses")

        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt

        text = nxt
        if prefill and not text.startswith(prefill):
            # Mirror the real client, which stitches the prefill back on.
            text = prefill + text
        return LLMResponse(text=text, model=model, input_tokens=100, output_tokens=200)


class FakeSEOClient:
    def __init__(self, metrics: dict[str, KeywordMetrics] | None = None, error: Exception | None = None):
        self.metrics = metrics or {}
        self.error = error
        self.calls: list[list[str]] = []

    def fetch_metrics(self, keywords: list[str]) -> dict[str, KeywordMetrics]:
        self.calls.append(keywords)
        if self.error:
            raise self.error
        return {
            k.strip().lower(): self.metrics.get(
                k.strip().lower(),
                KeywordMetrics(keyword=k, search_volume=500, keyword_difficulty=50),
            )
            for k in keywords
        }


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def profile(app):
    from app.models import BusinessProfile

    record = BusinessProfile(
        name="Frase",
        domain="frase.io",
        industry="SEO Content Tools",
        description="AI-powered content briefs",
        competitors=["surferseo.com", "clearscope.io"],
    )
    _db.session.add(record)
    _db.session.commit()
    return record
