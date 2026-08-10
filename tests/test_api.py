"""Endpoint contracts and orchestrator failure isolation."""

from __future__ import annotations

import json

import pytest

from app.agents import ContentRecommendationAgent, QueryDiscoveryAgent, VisibilityScoringAgent
from app.clients.llm import LLMError
from app.models import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    DiscoveredQuery,
)
from app.services.pipeline import PipelineOrchestrator
from tests.conftest import FakeLLM, FakeSEOClient


def make_orchestrator(app, llm, seo=None):
    return PipelineOrchestrator(
        discovery_agent=QueryDiscoveryAgent(llm, "m-discovery"),
        scoring_agent=VisibilityScoringAgent(llm, "m-scoring"),
        recommendation_agent=ContentRecommendationAgent(llm, "m-recommend"),
        seo_client=seo or FakeSEOClient(),
        config=app.config,
    )


DISCOVERY = json.dumps({
    "queries": [
        {"query_text": "Best AI content brief tool?", "commercial_intent": "comparison"},
        {"query_text": "Clearscope alternatives for small teams?", "commercial_intent": "comparison"},
    ]
})
RECOMMENDATIONS = json.dumps({
    "recommendations": [
        {
            "query_index": 0,
            "content_type": "comparison_page",
            "title": "Frase vs Surfer SEO for content teams",
            "rationale": "Surfer currently owns this answer.",
            "target_keywords": ["content brief tool"],
            "priority": "high",
        }
    ]
})
ANSWER_WITHOUT_TARGET = "Surfer SEO and Clearscope are the usual picks."


class TestProfileEndpoints:
    def test_create_profile(self, client):
        response = client.post(
            "/api/v1/profiles",
            json={
                "name": "Surfer SEO",
                "domain": "https://www.surferseo.com/",
                "industry": "SEO Software",
                "description": "AI-powered SEO optimisation",
                "competitors": ["clearscope.io", "marketmuse.com"],
            },
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["profile_uuid"]
        assert body["domain"] == "surferseo.com"  # normalised from the pasted URL
        assert body["status"] == "created"

    def test_rejects_invalid_domain(self, client):
        response = client.post(
            "/api/v1/profiles",
            json={"name": "X", "domain": "not a domain", "industry": "SEO"},
        )

        assert response.status_code == 422
        assert response.get_json()["error"]["code"] == "validation_error"

    def test_rejects_self_as_competitor(self, client):
        response = client.post(
            "/api/v1/profiles",
            json={
                "name": "X",
                "domain": "x.com",
                "industry": "SEO",
                "competitors": ["x.com"],
            },
        )

        assert response.status_code == 422

    def test_missing_required_field(self, client):
        response = client.post("/api/v1/profiles", json={"name": "X"})
        assert response.status_code == 422
        assert "domain" in response.get_json()["error"]["details"]

    def test_unknown_profile_returns_structured_404(self, client):
        response = client.get("/api/v1/profiles/does-not-exist")

        assert response.status_code == 404
        assert response.get_json()["error"]["code"] == "not_found"

    def test_profile_stats(self, client, profile):
        response = client.get(f"/api/v1/profiles/{profile.uuid}")

        assert response.status_code == 200
        stats = response.get_json()["stats"]
        assert stats["total_queries_discovered"] == 0
        assert stats["average_opportunity_score"] is None

    def test_correlation_id_is_echoed(self, client):
        response = client.get("/health", headers={"X-Correlation-ID": "abc-123"})
        assert response.headers["X-Correlation-ID"] == "abc-123"


class TestPipeline:
    def test_happy_path(self, app, profile):
        llm = FakeLLM([DISCOVERY, ANSWER_WITHOUT_TARGET, ANSWER_WITHOUT_TARGET, RECOMMENDATIONS])
        run = make_orchestrator(app, llm).run(profile)

        assert run.status == RUN_STATUS_COMPLETED
        assert run.queries_discovered == 2
        assert run.queries_scored == 2
        assert run.recommendations_generated == 1
        assert run.tokens_used == 4 * 300

    def test_one_failed_query_does_not_stop_the_batch(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            LLMError("provider timeout"),
            ANSWER_WITHOUT_TARGET,
            RECOMMENDATIONS,
        ])
        run = make_orchestrator(app, llm).run(profile)

        assert run.status == RUN_STATUS_PARTIAL
        assert run.queries_scored == 1
        assert any("scoring failed" in w for w in run.warnings)

        failed = DiscoveredQuery.query.filter(
            DiscoveredQuery.scoring_error.isnot(None)
        ).one()
        assert failed.visibility_status == "unknown"

    def test_discovery_failure_fails_the_run(self, app, profile):
        llm = FakeLLM([LLMError("provider down")])
        run = make_orchestrator(app, llm).run(profile)

        assert run.status == RUN_STATUS_FAILED
        assert "query discovery failed" in run.error_message
        assert run.completed_at is not None

    def test_dataforseo_outage_degrades_rather_than_fails(self, app, profile):
        from app.clients.dataforseo import DataForSEOError

        llm = FakeLLM([DISCOVERY, ANSWER_WITHOUT_TARGET, ANSWER_WITHOUT_TARGET, RECOMMENDATIONS])
        seo = FakeSEOClient(error=DataForSEOError("credits exhausted"))

        run = make_orchestrator(app, llm, seo).run(profile)

        assert run.status == RUN_STATUS_PARTIAL
        assert run.queries_scored == 2
        scored = DiscoveredQuery.query.first()
        assert scored.estimated_search_volume is None
        assert scored.opportunity_score is not None

    def test_recommendation_failure_keeps_scored_queries(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            ANSWER_WITHOUT_TARGET,
            ANSWER_WITHOUT_TARGET,
            LLMError("provider down"),
            LLMError("provider down"),
        ])
        run = make_orchestrator(app, llm).run(profile)

        assert run.status == RUN_STATUS_PARTIAL
        assert run.queries_scored == 2
        assert run.recommendations_generated == 0

    def test_rerun_reuses_existing_queries_instead_of_duplicating(self, app, profile):
        first = FakeLLM([DISCOVERY, ANSWER_WITHOUT_TARGET, ANSWER_WITHOUT_TARGET, RECOMMENDATIONS])
        make_orchestrator(app, first).run(profile)

        second = FakeLLM([DISCOVERY, ANSWER_WITHOUT_TARGET, ANSWER_WITHOUT_TARGET, RECOMMENDATIONS])
        make_orchestrator(app, second).run(profile)

        assert DiscoveredQuery.query.count() == 2


class TestQueryListing:
    @pytest.fixture
    def scored_profile(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            "Surfer SEO and Clearscope are the usual picks.",
            "Frase is the one I'd start with.",
            RECOMMENDATIONS,
        ])
        make_orchestrator(app, llm).run(profile)
        return profile

    def test_sorted_by_opportunity_descending(self, client, scored_profile):
        response = client.get(f"/api/v1/profiles/{scored_profile.uuid}/queries")
        scores = [q["opportunity_score"] for q in response.get_json()["queries"]]

        assert scores == sorted(scores, reverse=True)

    def test_min_score_filter(self, client, scored_profile):
        response = client.get(
            f"/api/v1/profiles/{scored_profile.uuid}/queries?min_score=0.99"
        )
        assert response.get_json()["queries"] == []

    def test_status_filter(self, client, scored_profile):
        response = client.get(
            f"/api/v1/profiles/{scored_profile.uuid}/queries?status=not_visible"
        )
        queries = response.get_json()["queries"]

        assert queries
        assert all(q["visibility_status"] == "not_visible" for q in queries)

    def test_invalid_status_is_rejected(self, client, scored_profile):
        response = client.get(
            f"/api/v1/profiles/{scored_profile.uuid}/queries?status=maybe"
        )
        assert response.status_code == 422

    def test_pagination(self, client, scored_profile):
        response = client.get(
            f"/api/v1/profiles/{scored_profile.uuid}/queries?page=1&per_page=1"
        )
        body = response.get_json()

        assert len(body["queries"]) == 1
        assert body["pagination"]["total"] == 2
        assert body["pagination"]["total_pages"] == 2

    def test_recommendations_endpoint(self, client, scored_profile):
        response = client.get(f"/api/v1/profiles/{scored_profile.uuid}/recommendations")
        body = response.get_json()

        assert response.status_code == 200
        assert body["recommendations"][0]["target_query_uuid"]
        assert body["recommendations"][0]["priority"] == "high"


class TestRecheck:
    def test_recheck_reports_the_change(self, app, client, profile, monkeypatch):
        llm = FakeLLM([DISCOVERY, ANSWER_WITHOUT_TARGET, ANSWER_WITHOUT_TARGET, RECOMMENDATIONS])
        make_orchestrator(app, llm).run(profile)

        query = DiscoveredQuery.query.first()
        assert query.domain_visible is False

        # The published-content scenario: the domain now appears in the answer.
        recheck_llm = FakeLLM(["Frase is the tool I'd recommend for this."])
        orchestrator = make_orchestrator(app, recheck_llm)
        monkeypatch.setattr(PipelineOrchestrator, "from_app", classmethod(lambda cls: orchestrator))

        response = client.post(f"/api/v1/queries/{query.uuid}/recheck")
        body = response.get_json()

        assert response.status_code == 200
        assert body["domain_visible"] is True
        assert body["previous"]["domain_visible"] is False
        assert body["changed"]["domain_visible"] is True

    def test_recheck_unknown_query(self, client):
        response = client.post("/api/v1/queries/nope/recheck")
        assert response.status_code == 404
