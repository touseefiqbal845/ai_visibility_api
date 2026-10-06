"""Audit rows written for analytics: call logs, stage handoffs, cost.

These tests pin the numbers a later scoring pass would query. They do not
cover host metrics; the audit does not record any.
"""

from __future__ import annotations

import pytest

from app.clients.dataforseo import DataForSEOError
from app.clients.llm import LLMError
from app.constants import (
    LLM_STATUS_ERROR,
    LLM_STATUS_SUCCESS,
    LLM_STATUS_UNPARSEABLE,
    PROFILE_STATUS_READY,
    RUN_KIND_PIPELINE,
    RUN_KIND_RECHECK,
    RUN_STATUS_FAILED,
    STAGE_DISCOVERY,
    STAGE_METRICS,
    STAGE_RECOMMENDATION,
    STAGE_SCORING,
)
from app.models import DiscoveredQuery, LlmCallLog, PipelineRun, PipelineStageStat
from app.services.pipeline import PipelineError
from app.utils.run_audit import estimate_llm_cost
from tests.test_api import (
    ANSWER_WITHOUT_TARGET,
    DISCOVERY,
    RECOMMENDATIONS,
    make_orchestrator,
)
from tests.conftest import FakeLLM, FakeSEOClient


# FakeLLM bills 100 input and 200 output tokens per successful call.
# TestingConfig prices unknown models at $1 / $2 per million.
CALL_COST = (100 * 1.0 + 200 * 2.0) / 1_000_000


def _stages(run) -> dict[str, PipelineStageStat]:
    rows = PipelineStageStat.query.filter_by(run_uuid=run.uuid).all()
    return {row.stage: row for row in rows}


class TestCostFormula:
    def test_known_model_uses_its_rate_and_unknown_uses_the_fallback(self):
        config = {
            "LLM_MODEL_PRICING": {"claude-haiku-4-5": (1.0, 5.0)},
            "LLM_PRICE_INPUT_PER_MILLION": 2.0,
            "LLM_PRICE_OUTPUT_PER_MILLION": 10.0,
        }

        assert estimate_llm_cost("claude-haiku-4-5", 1_000_000, 1_000_000, config) == 6.0
        assert estimate_llm_cost("custom-model", 1_000_000, 1_000_000, config) == 12.0


class TestPipelineAudit:
    def test_happy_path_logs_every_call_and_the_handoff(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            ANSWER_WITHOUT_TARGET,
            ANSWER_WITHOUT_TARGET,
            RECOMMENDATIONS,
        ])
        run = make_orchestrator(app, llm).run(profile)

        assert run.kind == RUN_KIND_PIPELINE
        assert run.llm_calls == 4
        assert run.llm_retries == 0
        assert run.llm_failures == 0
        assert run.estimated_cost_usd == round(4 * CALL_COST, 6)

        logs = LlmCallLog.query.filter_by(run_uuid=run.uuid).all()
        assert len(logs) == 4
        assert {row.status for row in logs} == {LLM_STATUS_SUCCESS}
        assert all(row.latency_ms >= 0 for row in logs)
        # Scoring calls point at the query they measured. The other agents do not.
        assert sum(row.query_uuid is not None for row in logs) == 2

        stages = _stages(run)
        assert set(stages) == {STAGE_DISCOVERY, STAGE_METRICS, STAGE_SCORING, STAGE_RECOMMENDATION}
        assert stages[STAGE_DISCOVERY].items_out == 2
        assert stages[STAGE_METRICS].items_in == stages[STAGE_DISCOVERY].items_out
        assert stages[STAGE_METRICS].calls == 1
        assert stages[STAGE_METRICS].estimated_cost_usd == 0.0
        assert stages[STAGE_SCORING].items_in == 2
        assert stages[STAGE_SCORING].items_out == 2
        assert stages[STAGE_SCORING].upstream_stage == STAGE_METRICS
        assert stages[STAGE_RECOMMENDATION].items_in == 2
        assert stages[STAGE_RECOMMENDATION].items_out == 1
        assert stages[STAGE_RECOMMENDATION].upstream_stage == STAGE_SCORING

        body = run.to_dict()
        assert body["llm_calls"] == 4
        assert [stage["stage"] for stage in body["stages"]] == [
            STAGE_DISCOVERY,
            STAGE_METRICS,
            STAGE_SCORING,
            STAGE_RECOMMENDATION,
        ]

    def test_json_retry_is_counted_separately_from_a_failure(self, app, profile):
        llm = FakeLLM([
            "not json",
            DISCOVERY,
            ANSWER_WITHOUT_TARGET,
            ANSWER_WITHOUT_TARGET,
            RECOMMENDATIONS,
        ])
        run = make_orchestrator(app, llm).run(profile)

        discovery = (
            LlmCallLog.query.filter_by(run_uuid=run.uuid, agent=STAGE_DISCOVERY)
            .order_by(LlmCallLog.attempt)
            .all()
        )
        assert [(row.attempt, row.status, row.is_retry) for row in discovery] == [
            (1, LLM_STATUS_UNPARSEABLE, False),
            (2, LLM_STATUS_SUCCESS, True),
        ]
        assert run.llm_calls == 5
        assert run.llm_retries == 1
        assert run.llm_failures == 0
        assert _stages(run)[STAGE_DISCOVERY].retries == 1
        assert _stages(run)[STAGE_DISCOVERY].failures == 0

    def test_scoring_failure_stays_on_the_run(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            LLMError("provider timeout"),
            ANSWER_WITHOUT_TARGET,
            RECOMMENDATIONS,
        ])
        run = make_orchestrator(app, llm).run(profile)

        failed = LlmCallLog.query.filter_by(run_uuid=run.uuid, status=LLM_STATUS_ERROR).one()
        assert failed.agent == STAGE_SCORING
        assert failed.query_uuid is not None
        assert "provider timeout" in failed.error_message
        assert failed.estimated_cost_usd == 0.0

        assert run.llm_failures == 1
        stage = _stages(run)[STAGE_SCORING]
        assert stage.failures == 1
        assert stage.items_in == 2
        assert stage.items_out == 1

    def test_failed_run_keeps_the_call_log(self, app, profile):
        llm = FakeLLM([LLMError("provider down")])
        run = make_orchestrator(app, llm).run(profile)

        stored = PipelineRun.query.filter_by(uuid=run.uuid).one()
        assert stored.status == RUN_STATUS_FAILED
        assert stored.llm_failures == 1
        log = LlmCallLog.query.filter_by(run_uuid=run.uuid).one()
        assert log.status == LLM_STATUS_ERROR
        assert _stages(run)[STAGE_DISCOVERY].failures == 1
        assert STAGE_SCORING not in _stages(run)

    def test_metrics_outage_is_a_stage_failure_not_an_llm_failure(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            ANSWER_WITHOUT_TARGET,
            ANSWER_WITHOUT_TARGET,
            RECOMMENDATIONS,
        ])
        seo = FakeSEOClient(error=DataForSEOError("credits exhausted"))
        run = make_orchestrator(app, llm, seo).run(profile)

        assert run.llm_failures == 0
        metrics = _stages(run)[STAGE_METRICS]
        assert metrics.failures == 1
        assert metrics.items_in == 2
        assert metrics.items_out == 0
        assert metrics.calls == 1
        assert "credits exhausted" in metrics.last_error


class TestRecheckAudit:
    def test_recheck_gets_its_own_run_and_does_not_flip_the_profile(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            ANSWER_WITHOUT_TARGET,
            ANSWER_WITHOUT_TARGET,
            RECOMMENDATIONS,
        ])
        make_orchestrator(app, llm).run(profile)
        query = DiscoveredQuery.query.first()

        recheck_llm = FakeLLM(["Frase is the tool I'd recommend for this."])
        make_orchestrator(app, recheck_llm).recheck(query)

        recheck_run = PipelineRun.query.filter_by(kind=RUN_KIND_RECHECK).one()
        assert recheck_run.llm_calls == 1
        assert recheck_run.queries_scored == 1
        assert recheck_run.estimated_cost_usd == round(CALL_COST, 6)
        log = LlmCallLog.query.filter_by(run_uuid=recheck_run.uuid).one()
        assert log.query_uuid == query.uuid
        assert log.agent == STAGE_SCORING
        assert profile.status == PROFILE_STATUS_READY

    def test_failed_recheck_is_audited_without_failing_the_profile(self, app, profile):
        llm = FakeLLM([
            DISCOVERY,
            ANSWER_WITHOUT_TARGET,
            ANSWER_WITHOUT_TARGET,
            RECOMMENDATIONS,
        ])
        make_orchestrator(app, llm).run(profile)
        query = DiscoveredQuery.query.first()

        recheck_llm = FakeLLM([LLMError("provider down")])
        with pytest.raises(PipelineError):
            make_orchestrator(app, recheck_llm).recheck(query)

        recheck_run = PipelineRun.query.filter_by(kind=RUN_KIND_RECHECK).one()
        assert recheck_run.status == RUN_STATUS_FAILED
        assert recheck_run.llm_failures == 1
        assert profile.status == PROFILE_STATUS_READY
