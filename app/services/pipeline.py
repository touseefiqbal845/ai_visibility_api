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
from app.constants import (
    LLM_STATUS_ERROR,
    RUN_KIND_RECHECK,
    STAGE_DISCOVERY,
    STAGE_METRICS,
    STAGE_RECOMMENDATION,
    STAGE_SCORING,
    STAGE_SEQUENCE,
    STAGE_UPSTREAM,
)
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
    LlmCallLog,
    PipelineRun,
    PipelineStageStat,
)
from app.models.base import utcnow
from app.utils.logging import set_correlation_id
from app.utils.run_audit import (
    AuditBuffer,
    StageRecord,
    clear_audit,
    current_audit,
    estimate_llm_cost,
    reset_audit_query,
    set_audit_query,
    start_audit,
)

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
        audit = start_audit()
        logger.info("pipeline started", extra={"profile_uuid": profile.uuid})

        try:
            try:
                queries = self._discover(profile, run, state)
                self._score_all(profile, run, queries, state)
                self._recommend(profile, run, state)
            except PipelineError as exc:
                self._finalise_failure(profile, run, state, audit, str(exc))
                return run
            except Exception as exc:  # noqa: BLE001 - the run record must reflect any failure
                logger.exception("pipeline crashed", extra={"profile_uuid": profile.uuid})
                self._finalise_failure(profile, run, state, audit, f"unexpected error: {exc}")
                return run

            self._apply_audit(run, audit)
            run.status = RUN_STATUS_PARTIAL if state.warnings else RUN_STATUS_COMPLETED
            run.warnings = state.warnings
            run.input_tokens = state.usage.input_tokens
            run.output_tokens = state.usage.output_tokens
            run.completed_at = utcnow()
            profile.status = PROFILE_STATUS_READY
            db.session.commit()
        finally:
            set_correlation_id(None)
            clear_audit()

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
            self._record_stage(STAGE_DISCOVERY, items_in=1, items_out=0, failures=1, error=str(exc))
            raise PipelineError(f"query discovery failed: {exc}") from exc

        state.usage.merge(result.usage)
        state.warnings.extend(result.warnings)

        drafts = result.data or []
        if not drafts:
            self._record_stage(
                STAGE_DISCOVERY,
                items_in=1,
                items_out=0,
                failures=1,
                error="query discovery returned no queries",
            )
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
        # items_in is the one profile brief. items_out is what scoring will receive.
        self._record_stage(STAGE_DISCOVERY, items_in=1, items_out=len(queries), failures=0)
        return queries

    def _fetch_metrics(
        self, queries: list[DiscoveredQuery], state: _RunState
    ) -> dict[str, KeywordMetrics]:
        """One batched call for the whole run rather than one per query."""
        try:
            metrics = self.seo_client.fetch_metrics([q.query_text for q in queries])
        except DataForSEOError as exc:
            state.warn(f"keyword metrics unavailable, scoring without them: {exc}")
            self._record_stage(
                STAGE_METRICS,
                items_in=len(queries),
                items_out=0,
                failures=1,
                calls=1,
                error=str(exc),
            )
            return {}

        # A row with no volume and no difficulty is not a handoff. The call still
        # succeeded, so this is not a stage failure.
        produced = sum(
            1
            for metric in metrics.values()
            if metric.search_volume is not None or metric.difficulty is not None
        )
        self._record_stage(
            STAGE_METRICS,
            items_in=len(queries),
            items_out=produced,
            failures=0,
            calls=1,
        )
        return metrics

    def _score_all(
        self,
        profile: BusinessProfile,
        run: PipelineRun,
        queries: list[DiscoveredQuery],
        state: _RunState,
    ) -> None:
        metrics = self._fetch_metrics(queries, state)
        scored = 0
        last_error: str | None = None

        for query in queries:
            try:
                self._score_one(profile, query, metrics, state)
                scored += 1
            except (AgentError, LLMError) as exc:
                # One bad query must not cost us the other fourteen.
                last_error = str(exc)
                query.scoring_error = last_error[:1000]
                query.visibility_status = VISIBILITY_UNKNOWN
                state.warn(f"scoring failed for {query.query_text[:60]!r}: {exc}")

        failed = len(queries) - scored
        self._record_stage(
            STAGE_SCORING,
            items_in=len(queries),
            items_out=scored,
            failures=failed,
            error=last_error if failed else None,
        )
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
        token = set_audit_query(query.uuid)
        try:
            result = self.scoring_agent.score(
                query_text=query.query_text,
                commercial_intent=query.commercial_intent,
                target_domain=profile.domain,
                target_name=profile.name,
                competitors=profile.competitors or [],
                metrics=metrics.get(query.query_text.strip().lower()),
            )
        finally:
            reset_audit_query(token)
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
            self._record_stage(STAGE_RECOMMENDATION, items_in=0, items_out=0, failures=0)
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
            self._record_stage(
                STAGE_RECOMMENDATION,
                items_in=len(contexts),
                items_out=0,
                failures=1,
                error=str(exc),
            )
            return

        state.usage.merge(result.usage)
        state.warnings.extend(result.warnings)
        produced = len(result.data or [])
        self._record_stage(
            STAGE_RECOMMENDATION,
            items_in=len(contexts),
            items_out=produced,
            failures=0 if produced else 1,
            error=None if produced else "no usable recommendations",
        )

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
        """Re-run Agent 2 for one query, e.g. after content has been published.

        Writes its own pipeline_runs row (kind=recheck) so the call, the cost and
        any failure are queryable. Does not change the profile's lifecycle status.
        """
        profile = query.profile
        run = PipelineRun(
            profile_uuid=profile.uuid,
            kind=RUN_KIND_RECHECK,
            status=RUN_STATUS_RUNNING,
            started_at=utcnow(),
        )
        db.session.add(run)
        db.session.commit()

        set_correlation_id(run.uuid)
        state = _RunState()
        audit = start_audit()

        try:
            try:
                metrics = self._fetch_metrics([query], state)
                self._score_one(profile, query, metrics, state)
            except (AgentError, LLMError) as exc:
                self._record_stage(
                    STAGE_SCORING, items_in=1, items_out=0, failures=1, error=str(exc)
                )
                self._finalise_recheck_failure(run, state, audit, str(exc))
                raise PipelineError(f"recheck failed: {exc}") from exc

            self._record_stage(STAGE_SCORING, items_in=1, items_out=1, failures=0)
            self._apply_audit(run, audit)
            run.status = RUN_STATUS_PARTIAL if state.warnings else RUN_STATUS_COMPLETED
            run.warnings = state.warnings
            run.queries_scored = 1
            run.input_tokens = state.usage.input_tokens
            run.output_tokens = state.usage.output_tokens
            run.completed_at = utcnow()
            db.session.commit()
        finally:
            set_correlation_id(None)
            clear_audit()

        return query

    # --- audit ---------------------------------------------------------------

    def _record_stage(
        self,
        stage: str,
        *,
        items_in: int,
        items_out: int,
        failures: int,
        error: str | None = None,
        calls: int | None = None,
    ) -> None:
        audit = current_audit()
        if audit is None:
            return
        message = (error or "").strip()
        audit.stages.append(
            StageRecord(
                stage=stage,
                sequence=STAGE_SEQUENCE[stage],
                upstream_stage=STAGE_UPSTREAM[stage],
                items_in=items_in,
                items_out=items_out,
                failures=failures,
                last_error=message[:1000] or None,
                calls=calls,
            )
        )

    def _apply_audit(self, run: PipelineRun, audit: AuditBuffer) -> None:
        """Copy the in-memory buffer onto the run. Does not commit."""
        total_cost = 0.0
        for call in audit.calls:
            cost = estimate_llm_cost(
                call.model, call.input_tokens, call.output_tokens, self.config
            )
            total_cost += cost
            db.session.add(
                LlmCallLog(
                    run_uuid=run.uuid,
                    profile_uuid=run.profile_uuid,
                    query_uuid=call.query_uuid,
                    agent=call.agent,
                    model=call.model,
                    attempt=call.attempt,
                    is_retry=call.is_retry,
                    status=call.status,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    latency_ms=call.latency_ms,
                    estimated_cost_usd=cost,
                    error_message=call.error_message,
                )
            )

        for stage in audit.stages:
            calls = audit.calls_for(stage.stage)
            stage_cost = round(
                sum(
                    estimate_llm_cost(call.model, call.input_tokens, call.output_tokens, self.config)
                    for call in calls
                ),
                6,
            )
            db.session.add(
                PipelineStageStat(
                    run_uuid=run.uuid,
                    profile_uuid=run.profile_uuid,
                    stage=stage.stage,
                    sequence=stage.sequence,
                    upstream_stage=stage.upstream_stage,
                    items_in=stage.items_in,
                    items_out=stage.items_out,
                    calls=stage.calls if stage.calls is not None else len(calls),
                    retries=sum(1 for call in calls if call.is_retry),
                    failures=stage.failures,
                    input_tokens=sum(call.input_tokens for call in calls),
                    output_tokens=sum(call.output_tokens for call in calls),
                    latency_ms=sum(call.latency_ms for call in calls),
                    estimated_cost_usd=stage_cost,
                    last_error=stage.last_error,
                )
            )

        run.llm_calls = len(audit.calls)
        run.llm_retries = sum(1 for call in audit.calls if call.is_retry)
        run.llm_failures = sum(1 for call in audit.calls if call.status == LLM_STATUS_ERROR)
        run.estimated_cost_usd = round(total_cost, 6)

    # --- failure handling ----------------------------------------------------

    def _finalise_failure(
        self,
        profile: BusinessProfile,
        run: PipelineRun,
        state: _RunState,
        audit: AuditBuffer,
        message: str,
    ) -> None:
        run_uuid = run.uuid
        profile_uuid = profile.uuid
        db.session.rollback()
        # Re-attach after the rollback, then record the failure in its own transaction.
        # The audit buffer is in memory, so the rollback does not drop it.
        run = db.session.get(PipelineRun, run_uuid)
        profile = db.session.get(BusinessProfile, profile_uuid)
        if run is not None:
            run.status = RUN_STATUS_FAILED
            run.error_message = message[:2000]
            run.warnings = state.warnings
            run.input_tokens = state.usage.input_tokens
            run.output_tokens = state.usage.output_tokens
            run.completed_at = utcnow()
            self._apply_audit(run, audit)
        if profile is not None:
            profile.status = PROFILE_STATUS_FAILED
        db.session.commit()
        logger.error("pipeline failed", extra={"reason": message})

    def _finalise_recheck_failure(
        self, run: PipelineRun, state: _RunState, audit: AuditBuffer, message: str
    ) -> None:
        run_uuid = run.uuid
        db.session.rollback()
        run = db.session.get(PipelineRun, run_uuid)
        if run is None:
            return
        run.status = RUN_STATUS_FAILED
        run.error_message = message[:2000]
        run.warnings = state.warnings
        run.input_tokens = state.usage.input_tokens
        run.output_tokens = state.usage.output_tokens
        run.completed_at = utcnow()
        self._apply_audit(run, audit)
        db.session.commit()


__all__ = ["PipelineOrchestrator", "PipelineError", "build_agents", "build_dataforseo"]
