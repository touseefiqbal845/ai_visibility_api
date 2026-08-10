"""Agent behaviour against mocked model output.

The cases worth testing here are the ones that bite in production: malformed JSON,
partially malformed JSON, and the visibility detection logic.
"""

from __future__ import annotations

import json

import pytest

from app.agents.base import AgentError
from app.agents.discovery import QueryDiscoveryAgent
from app.agents.recommendation import ContentRecommendationAgent, QueryContext
from app.agents.scoring import VisibilityScoringAgent
from app.clients.dataforseo import KeywordMetrics
from app.constants import INTENT_COMPARISON, INTENT_INFORMATIONAL
from tests.conftest import FakeLLM

PROFILE_KWARGS = {
    "name": "Frase",
    "domain": "frase.io",
    "industry": "SEO Content Tools",
    "description": "AI content briefs",
    "competitors": ["surferseo.com", "clearscope.io"],
}


def discovery_payload(queries):
    return json.dumps({"queries": queries})


class TestQueryDiscoveryAgent:
    def test_parses_well_formed_response(self):
        llm = FakeLLM([
            discovery_payload([
                {"query_text": "Best AI tool for SEO content briefs?", "commercial_intent": "comparison"},
                {"query_text": "How much do content brief tools cost?", "commercial_intent": "commercial"},
            ])
        ])
        agent = QueryDiscoveryAgent(llm, "test-model")

        result = agent.run(**PROFILE_KWARGS, count=2)

        assert len(result.data) == 2
        assert result.data[0].commercial_intent == INTENT_COMPARISON
        assert result.usage.calls == 1
        assert result.usage.total == 300

    def test_recovers_json_wrapped_in_prose_and_fences(self):
        wrapped = (
            "Sure, here are the queries you asked for:\n\n```json\n"
            + discovery_payload([{"query_text": "Surfer SEO vs Clearscope?", "commercial_intent": "comparison"}])
            + "\n```\nLet me know if you want more."
        )
        agent = QueryDiscoveryAgent(FakeLLM([wrapped]), "test-model")

        result = agent.run(**PROFILE_KWARGS, count=1)

        assert len(result.data) == 1
        assert result.data[0].query_text == "Surfer SEO vs Clearscope?"

    def test_retries_once_then_succeeds(self):
        llm = FakeLLM([
            "I'm not able to produce that as JSON.",
            discovery_payload([{"query_text": "Best SEO brief tool?", "commercial_intent": "comparison"}]),
        ])
        agent = QueryDiscoveryAgent(llm, "test-model")

        result = agent.run(**PROFILE_KWARGS, count=1)

        assert len(result.data) == 1
        assert llm.calls[1]["user"] != llm.calls[0]["user"]  # corrective prompt differs
        assert result.usage.calls == 2

    def test_raises_after_second_failure(self):
        agent = QueryDiscoveryAgent(FakeLLM(["not json", "still not json"]), "test-model")

        with pytest.raises(AgentError):
            agent.run(**PROFILE_KWARGS, count=1)

    def test_drops_bad_items_but_keeps_the_rest(self):
        llm = FakeLLM([
            discovery_payload([
                {"query_text": "Best SEO brief tool?", "commercial_intent": "comparison"},
                {"query_text": "", "commercial_intent": "comparison"},
                {"commercial_intent": "comparison"},
                {"query_text": "What is a content brief?", "commercial_intent": "nonsense"},
            ])
        ])
        agent = QueryDiscoveryAgent(llm, "test-model")

        result = agent.run(**PROFILE_KWARGS, count=4)

        assert [d.query_text for d in result.data] == [
            "Best SEO brief tool?",
            "What is a content brief?",
        ]
        assert result.data[1].commercial_intent == INTENT_INFORMATIONAL
        assert len(result.warnings) == 3

    def test_deduplicates_case_insensitively(self):
        llm = FakeLLM([
            discovery_payload([
                {"query_text": "Best SEO tool?", "commercial_intent": "comparison"},
                {"query_text": "  best seo tool?  ", "commercial_intent": "comparison"},
            ])
        ])
        agent = QueryDiscoveryAgent(llm, "test-model")

        assert len(agent.run(**PROFILE_KWARGS, count=2).data) == 1


class TestVisibilityScoringAgent:
    def _score(self, answer: str, **overrides):
        agent = VisibilityScoringAgent(FakeLLM([answer]), "test-model")
        kwargs = {
            "query_text": "Best AI content brief tool?",
            "commercial_intent": INTENT_COMPARISON,
            "target_domain": "frase.io",
            "target_name": "Frase",
            "competitors": ["surferseo.com", "clearscope.io"],
            "metrics": KeywordMetrics(keyword="q", search_volume=1200, keyword_difficulty=62),
        }
        kwargs.update(overrides)
        return agent.score(**kwargs).data

    def test_detects_target_and_orders_by_first_mention(self):
        result = self._score(
            "I'd start with Surfer SEO for on-page work. Frase is stronger for briefs, "
            "and Clearscope is the enterprise option."
        )

        assert result.domain_visible is True
        assert result.visibility_position == 2
        assert result.competitors_visible == ["surferseo.com", "clearscope.io"]

    def test_absent_domain_scores_higher_than_present_one(self):
        absent = self._score("Surfer SEO and Clearscope are the two I'd look at.")
        present = self._score("Frase is the one I'd recommend first.")

        assert absent.domain_visible is False
        assert absent.visibility_position is None
        assert absent.opportunity_score > present.opportunity_score

    def test_matches_spaced_and_capitalised_brand_forms(self):
        result = self._score(
            "Surfer SEO is the best known option here.",
            target_domain="surferseo.com",
            target_name="Surfer SEO",
            competitors=["frase.io"],
        )

        assert result.domain_visible is True
        assert result.visibility_position == 1

    def test_does_not_match_a_brand_inside_another_word(self):
        # "frase" sits inside "phrase"; a naive substring check would report a hit.
        result = self._score("Think carefully about the phrase you target.")

        assert result.domain_visible is False

    def test_missing_metrics_still_produces_a_score(self):
        result = self._score("Surfer SEO is the main option.", metrics=None)

        assert result.search_volume is None
        assert 0.0 <= result.opportunity_score <= 1.0
        assert "volume" in result.breakdown.notes[0]


class TestContentRecommendationAgent:
    contexts = [
        QueryContext("uuid-a", "Best AI content brief tool?", 0.82, ["surferseo.com"]),
        QueryContext("uuid-b", "Frase alternatives?", 0.71, ["clearscope.io"]),
    ]

    def _run(self, payload):
        agent = ContentRecommendationAgent(FakeLLM([json.dumps(payload)]), "test-model")
        return agent.run(
            name="Frase",
            domain="frase.io",
            industry="SEO Content Tools",
            description=None,
            queries=self.contexts,
            count=2,
        )

    def test_maps_recommendations_back_by_index(self):
        result = self._run({
            "recommendations": [
                {
                    "query_index": 1,
                    "content_type": "comparison_page",
                    "title": "Frase vs Clearscope for content teams",
                    "rationale": "Clearscope currently owns this answer.",
                    "target_keywords": ["frase alternatives", "content brief tool"],
                    "priority": "high",
                }
            ]
        })

        assert result.data[0].query_index == 1
        assert result.data[0].content_type == "comparison_page"

    def test_drops_out_of_range_and_duplicate_indexes(self):
        result = self._run({
            "recommendations": [
                {"query_index": 0, "content_type": "blog_post", "title": "A", "rationale": "R"},
                {"query_index": 0, "content_type": "blog_post", "title": "B", "rationale": "R"},
                {"query_index": 9, "content_type": "blog_post", "title": "C", "rationale": "R"},
                {"query_index": "x", "content_type": "blog_post", "title": "D", "rationale": "R"},
            ]
        })

        assert [d.title for d in result.data] == ["A"]
        assert len(result.warnings) == 3

    def test_falls_back_on_unknown_content_type_and_priority(self):
        result = self._run({
            "recommendations": [
                {
                    "query_index": 0,
                    "content_type": "whitepaper",
                    "title": "A",
                    "rationale": "R",
                    "priority": "urgent",
                }
            ]
        })

        assert result.data[0].content_type == "blog_post"
        assert result.data[0].priority == "medium"

    def test_no_gaps_means_no_call(self):
        llm = FakeLLM([])
        agent = ContentRecommendationAgent(llm, "test-model")

        result = agent.run(
            name="Frase", domain="frase.io", industry="SEO", description=None, queries=[]
        )

        assert result.data == []
        assert llm.calls == []
