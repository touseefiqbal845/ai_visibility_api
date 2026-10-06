"""In-memory audit buffer for a single pipeline run.

Agents append one record per model call. The orchestrator appends one record per
stage handoff, then writes both to the database when the run ends. Records stay
in memory until then: the failure path rolls the session back, and anything
already added to the session would be lost with it.
"""

from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass
class LlmCallRecord:
    agent: str
    model: str
    attempt: int
    is_retry: bool
    status: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error_message: str | None = None
    query_uuid: str | None = None


@dataclass
class StageRecord:
    """One handoff. `items_in` is what the previous stage passed across,
    `items_out` is what this stage handed on. `calls` overrides the LLM-call
    count for stages that are not model calls (DataForSEO).
    """

    stage: str
    sequence: int
    upstream_stage: str | None
    items_in: int
    items_out: int
    failures: int
    last_error: str | None = None
    calls: int | None = None


@dataclass
class AuditBuffer:
    calls: list[LlmCallRecord] = field(default_factory=list)
    stages: list[StageRecord] = field(default_factory=list)

    def calls_for(self, agent: str) -> list[LlmCallRecord]:
        return [call for call in self.calls if call.agent == agent]


_audit: ContextVar[AuditBuffer | None] = ContextVar("run_audit", default=None)
_query_uuid: ContextVar[str | None] = ContextVar("audit_query_uuid", default=None)


def start_audit() -> AuditBuffer:
    buffer = AuditBuffer()
    _audit.set(buffer)
    _query_uuid.set(None)
    return buffer


def current_audit() -> AuditBuffer | None:
    return _audit.get()


def clear_audit() -> None:
    _audit.set(None)
    _query_uuid.set(None)


def set_audit_query(query_uuid: str | None) -> Token:
    return _query_uuid.set(query_uuid)


def reset_audit_query(token: Token) -> None:
    _query_uuid.reset(token)


def record_llm_call(
    *,
    agent: str,
    model: str,
    attempt: int,
    status: str,
    started: float,
    response=None,
    error: str | None = None,
) -> None:
    """Record one model call if a run audit is open. No-op otherwise.

    `started` is a `time.perf_counter()` value taken just before the call.
    Latency is the call itself, not host CPU or memory.
    """
    buffer = _audit.get()
    if buffer is None:
        return

    message = (error or "").strip()
    buffer.calls.append(
        LlmCallRecord(
            agent=agent,
            model=model,
            attempt=attempt,
            is_retry=attempt > 1,
            status=status,
            input_tokens=getattr(response, "input_tokens", 0) or 0,
            output_tokens=getattr(response, "output_tokens", 0) or 0,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            error_message=message[:1000] or None,
            query_uuid=_query_uuid.get(),
        )
    )


def estimate_llm_cost(model: str, input_tokens: int, output_tokens: int, config) -> float:
    """USD list-price estimate. Not an invoice: cache and batch rates are ignored."""
    table = config.get("LLM_MODEL_PRICING") or {}
    rates = table.get(model)
    if rates is None:
        rates = (
            float(config.get("LLM_PRICE_INPUT_PER_MILLION", 2.0)),
            float(config.get("LLM_PRICE_OUTPUT_PER_MILLION", 10.0)),
        )
    input_rate, output_rate = rates
    raw = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return round(raw, 6)
