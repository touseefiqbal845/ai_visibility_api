"""Pipeline orchestrator: Agent 1 -> DataForSEO -> Agent 2 -> Agent 3.

Failure policy, in short:

  Agent 1 fails            -> the run fails. There is nothing downstream to do.
  DataForSEO fails         -> the run continues without volume/difficulty. The score
                              formula redistributes those weights and the run is
                              marked completed_with_errors.
  Agent 2 fails on a query -> that query is marked and the loop continues.
  Agent 3 fails            -> queries and scores are kept, recommendations are empty,
                              run is completed_with_errors.

The distinction that matters is between "we produced nothing" and "we produced less
than we wanted". Only the first is a failed run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from flask import current_app

from app.agents import (
    AgentError,
    ContentRecommendationAgent,
    QueryContext,
    QueryDiscoveryAgent,
    TokenUsage,
    VisibilityScoringAgent,
)
from app.clients.dataforseo import DataForSEOClient, DataForSEOError, KeywordMetrics
from app.clients.llm import AnthropicClient, LLMError
from app.extensions import db
from app.models import (
    PROFILE_STATUS_FAILED,
    PROFILE_STATUS_READY,
    PROFILE_STATUS_RUNNING,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_RUNNING,
    VISIBILITY_UNKNOWN,
    BusinessProfile,
    ContentRecommendation,
    DiscoveredQuery,
    PipelineRun,
)
from app.models.base import utcnow
from app.utils.logging import set_correlation_id

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """The run could not produce any usable output."""


@dataclass
class _RunState:
    usage: TokenUsage = field(default_factory=TokenUsage)
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        logger.warning("pipeline warning", extra={"detail": message})
        self.warnings.append(message)


def build_agents(config) -> tuple[QueryDiscoveryAgent, VisibilityScoringAgent, ContentRecommendationAgent]:
    llm = AnthropicClient(
        config["ANTHROPIC_API_KEY"],
        timeout=config["LLM_TIMEOUT_SECONDS"],
        max_retries=config["LLM_MAX_RETRIES"],
    )
    return (
        QueryDiscoveryAgent(llm, config["MODEL_DISCOVERY"]),
        VisibilityScoringAgent(llm, config["MODEL_SCORING"]),
        ContentRecommendationAgent(llm, config["MODEL_RECOMMENDATION"]),
    )


def build_dataforseo(config) -> DataForSEOClient:
    return DataForSEOClient(
        config["DATAFORSEO_LOGIN"],
        config["DATAFORSEO_PASSWORD"],
        base_url=config["DATAFORSEO_BASE_URL"],
        location_code=config["DATAFORSEO_LOCATION_CODE"],
        language_code=config["DATAFORSEO_LANGUAGE_CODE"],
        timeout=config["DATAFORSEO_TIMEOUT_SECONDS"],
    )


class PipelineOrchestrator:
    def __init__(
        self,
        *,
        discovery_agent: QueryDiscoveryAgent,
        scoring_agent: VisibilityScoringAgent,
        recommendation_agent: ContentRecommendationAgent,
        seo_client: DataForSEOClient,
        config: dict | None = None,
    ) -> None:
        self.discovery_agent = discovery_agent
        self.scoring_agent = scoring_agent
        self.recommendation_agent = recommendation_agent
        self.seo_client = seo_client
        self.config = config if config is not None else current_app.config

    @classmethod
    def from_app(cls) -> "PipelineOrchestrator":
        config = current_app.config
        discovery, scoring, recommendation = build_agents(config)
        return cls(
            discovery_agent=discovery,
            scoring_agent=scoring,
            recommendation_agent=recommendation,
            seo_client=build_dataforseo(config),
            config=config,
        )

    # --- entry point ---------------------------------------------------------

    def run(self, profile: BusinessProfile) -> PipelineRun:
        run = PipelineRun(
            profile_uuid=profile.uuid,
            status=RUN_STATUS_RUNNING,
            started_at=utcnow(),
        )
        db.session.add(run)
        profile.status = PROFILE_STATUS_RUNNING
        db.session.commit()

        set_correlation_id(run.uuid)
        state = _RunState()
        logger.info("pipeline started", extra={"profile_uuid": profile.uuid})

        try:
            queries = self._discover(profile, run, state)
            self._score_all(profile, run, queries, state)
            self._recommend(profile, run, state)
        except PipelineError as exc:
            self._finalise_failure(profile, run, state, str(exc))
            return run
        except Exception as exc:  # noqa: BLE001 - the run record must reflect any failure
            logger.exception("pipeline crashed", extra={"profile_uuid": profile.uuid})
            self._finalise_failure(profile, run, state, f"unexpected error: {exc}")
            return run
        finally:
            set_correlation_id(None)

        run.status = RUN_STATUS_PARTIAL if state.warnings else RUN_STATUS_COMPLETED
        run.warnings = state.warnings
        run.input_tokens = state.usage.input_tokens
        run.output_tokens = state.usage.output_tokens
        run.completed_at = utcnow()
        profile.status = PROFILE_STATUS_READY
        db.session.commit()

        logger.info(
            "pipeline finished",
            extra={
                "status": run.status,
                "queries_discovered": run.queries_discovered,
                "queries_scored": run.queries_scored,
                "tokens": run.tokens_used,
            },
        )
        return run

    # --- stages --------------------------------------------------------------

    def _discover(
        self, profile: BusinessProfile, run: PipelineRun, state: _RunState
    ) -> list[DiscoveredQuery]:
        try:
            result = self.discovery_agent.run(
                name=profile.name,
                domain=profile.domain,
                industry=profile.industry,
                description=profile.description,
                competitors=profile.competitors or [],
                count=self.config["DISCOVERY_TARGET_QUERIES"],
            )
        except (AgentError, LLMError) as exc:
            raise PipelineError(f"query discovery failed: {exc}") from exc

        state.usage.merge(result.usage)
        state.warnings.extend(result.warnings)

        drafts = result.data or []
        if not drafts:
            raise PipelineError("query discovery returned no queries")

        # A profile can be run repeatedly; queries seen before are reused so their
        # history survives rather than being duplicated under a new uuid.
        existing = {
            q.query_text.strip().lower(): q
            for q in DiscoveredQuery.query.filter_by(profile_uuid=profile.uuid).all()
        }

        queries: list[DiscoveredQuery] = []
        for draft in drafts:
            key = draft.query_text.strip().lower()
            query = existing.get(key)
            if query is None:
                query = DiscoveredQuery(
                    profile_uuid=profile.uuid,
                    query_text=draft.query_text,
                    commercial_intent=draft.commercial_intent,
                    visibility_status=VISIBILITY_UNKNOWN,
                    discovered_at=utcnow(),
                )
                db.session.add(query)
            query.run_uuid = run.uuid
            query.commercial_intent = draft.commercial_intent
            queries.append(query)

        run.queries_discovered = len(queries)
        db.session.commit()
        return queries

    def _fetch_metrics(
        self, queries: list[DiscoveredQuery], state: _RunState
    ) -> dict[str, KeywordMetrics]:
        """One batched call for the whole run rather than one per query."""
        try:
            return self.seo_client.fetch_metrics([q.query_text for q in queries])
        except DataForSEOError as exc:
            state.warn(f"keyword metrics unavailable, scoring without them: {exc}")
            return {}

    def _score_all(
        self,
        profile: BusinessProfile,
        run: PipelineRun,
        queries: list[DiscoveredQuery],
        state: _RunState,
    ) -> None:
        metrics = self._fetch_metrics(queries, state)
        scored = 0

        for query in queries:
            try:
                self._score_one(profile, query, metrics, state)
                scored += 1
            except (AgentError, LLMError) as exc:
                # One bad query must not cost us the other fourteen.
                query.scoring_error = str(exc)[:1000]
                query.visibility_status = VISIBILITY_UNKNOWN
                state.warn(f"scoring failed for {query.query_text[:60]!r}: {exc}")

        run.queries_scored = scored
        db.session.commit()

        if scored == 0:
            raise PipelineError("no queries could be scored")

    def _score_one(
        self,
        profile: BusinessProfile,
        query: DiscoveredQuery,
        metrics: dict[str, KeywordMetrics],
        state: _RunState,
    ) -> None:
        result = self.scoring_agent.score(
            query_text=query.query_text,
            commercial_intent=query.commercial_intent,
            target_domain=profile.domain,
            target_name=profile.name,
            competitors=profile.competitors or [],
            metrics=metrics.get(query.query_text.strip().lower()),
        )
        state.usage.merge(result.usage)

        visibility = result.data
        query.estimated_search_volume = visibility.search_volume
        query.competitive_difficulty = visibility.competitive_difficulty
        query.opportunity_score = visibility.opportunity_score
        query.domain_visible = visibility.domain_visible
        query.visibility_position = visibility.visibility_position
        query.visibility_status = visibility.visibility_status
        query.competitors_visible = visibility.competitors_visible
        query.answer_excerpt = visibility.answer_excerpt
        query.scoring_error = None
        query.last_checked_at = utcnow()

    def _recommend(
        self, profile: BusinessProfile, run: PipelineRun, state: _RunState
    ) -> None:
        gaps = (
            DiscoveredQuery.query.filter_by(profile_uuid=profile.uuid, domain_visible=False)
            .filter(DiscoveredQuery.opportunity_score.isnot(None))
            .order_by(DiscoveredQuery.opportunity_score.desc())
            .limit(self.config["RECOMMENDATION_INPUT_QUERIES"])
            .all()
        )

        if not gaps:
            state.warn("no visibility gaps found, skipping recommendations")
            return

        contexts = [
            QueryContext(
                query_uuid=q.uuid,
                query_text=q.query_text,
                opportunity_score=q.opportunity_score,
                competitors_visible=q.competitors_visible or [],
            )
            for q in gaps
        ]

        try:
            result = self.recommendation_agent.run(
                name=profile.name,
                domain=profile.domain,
                industry=profile.industry,
                description=profile.description,
                queries=contexts,
                count=self.config["RECOMMENDATION_COUNT"],
            )
        except (AgentError, LLMError) as exc:
            # Scored queries are already persisted and useful on their own.
            state.warn(f"recommendation generation failed: {exc}")
            return

        state.usage.merge(result.usage)
        state.warnings.extend(result.warnings)

        # A rerun replaces the previous recommendations rather than stacking them.
        ContentRecommendation.query.filter_by(profile_uuid=profile.uuid).delete()

        for draft in result.data or []:
            context = contexts[draft.query_index]
            db.session.add(
                ContentRecommendation(
                    profile_uuid=profile.uuid,
                    query_uuid=context.query_uuid,
                    run_uuid=run.uuid,
                    content_type=draft.content_type,
                    title=draft.title,
                    rationale=draft.rationale,
                    target_keywords=draft.target_keywords,
                    priority=draft.priority,
                )
            )

        run.recommendations_generated = len(result.data or [])
        db.session.commit()

    # --- single-query re-check ----------------------------------------------

    def recheck(self, query: DiscoveredQuery) -> DiscoveredQuery:
        """Re-run Agent 2 for one query, e.g. after content has been published."""
        profile = query.profile
        set_correlation_id(query.uuid)
        state = _RunState()

        try:
            metrics = self._fetch_metrics([query], state)
            self._score_one(profile, query, metrics, state)
            db.session.commit()
        except (AgentError, LLMError) as exc:
            db.session.rollback()
            raise PipelineError(f"recheck failed: {exc}") from exc
        finally:
            set_correlation_id(None)

        return query

    # --- failure handling ----------------------------------------------------

    def _finalise_failure(
        self, profile: BusinessProfile, run: PipelineRun, state: _RunState, message: str
    ) -> None:
        db.session.rollback()
        # Re-attach after the rollback, then record the failure in its own transaction.
        run = db.session.get(PipelineRun, run.uuid)
        profile = db.session.get(BusinessProfile, profile.uuid)
        if run is not None:
            run.status = RUN_STATUS_FAILED
            run.error_message = message[:2000]
            run.warnings = state.warnings
            run.input_tokens = state.usage.input_tokens
            run.output_tokens = state.usage.output_tokens
            run.completed_at = utcnow()
        if profile is not None:
            profile.status = PROFILE_STATUS_FAILED
        db.session.commit()
        logger.error("pipeline failed", extra={"reason": message})


__all__ = ["PipelineOrchestrator", "PipelineError", "build_agents", "build_dataforseo"]
