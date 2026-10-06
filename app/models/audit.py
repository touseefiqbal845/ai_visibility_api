"""Queryable audit rows for one pipeline run.

`llm_call_logs` is one row per model call (including the corrective retry and
hard failures). `pipeline_stage_stats` is one row per stage, including the
DataForSEO handoff, so a query can follow what each stage received and produced.

Prompt and response bodies are not stored. The answer text already lives on
`discovered_queries.answer_excerpt`; these tables are for counts, failures,
retries and cost.
"""

from __future__ import annotations

from typing import Any

from app.extensions import db
from app.models.base import UUIDMixin, iso, utcnow


class LlmCallLog(UUIDMixin, db.Model):
    __tablename__ = "llm_call_logs"

    run_uuid = db.Column(
        db.String(36),
        db.ForeignKey("pipeline_runs.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_uuid = db.Column(
        db.String(36),
        db.ForeignKey("business_profiles.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Set for scoring calls so a failure can be joined back to the query.
    query_uuid = db.Column(
        db.String(36),
        db.ForeignKey("discovered_queries.uuid", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    agent = db.Column(db.String(64), nullable=False, index=True)
    model = db.Column(db.String(128), nullable=False)
    attempt = db.Column(db.Integer, nullable=False, default=1)
    is_retry = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(16), nullable=False, index=True)

    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    latency_ms = db.Column(db.Integer, nullable=False, default=0)
    estimated_cost_usd = db.Column(db.Float, nullable=False, default=0.0)

    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    run = db.relationship("PipelineRun", back_populates="call_logs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_uuid": self.uuid,
            "run_uuid": self.run_uuid,
            "profile_uuid": self.profile_uuid,
            "query_uuid": self.query_uuid,
            "agent": self.agent,
            "model": self.model,
            "attempt": self.attempt,
            "is_retry": self.is_retry,
            "status": self.status,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "estimated_cost_usd": self.estimated_cost_usd,
            "error_message": self.error_message,
            "created_at": iso(self.created_at),
        }


class PipelineStageStat(UUIDMixin, db.Model):
    __tablename__ = "pipeline_stage_stats"

    run_uuid = db.Column(
        db.String(36),
        db.ForeignKey("pipeline_runs.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_uuid = db.Column(
        db.String(36),
        db.ForeignKey("business_profiles.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    stage = db.Column(db.String(64), nullable=False, index=True)
    sequence = db.Column(db.Integer, nullable=False)
    upstream_stage = db.Column(db.String(64), nullable=True)

    # Collaboration: what the upstream stage handed in, and what this stage handed on.
    items_in = db.Column(db.Integer, nullable=False, default=0)
    items_out = db.Column(db.Integer, nullable=False, default=0)

    calls = db.Column(db.Integer, nullable=False, default=0)
    retries = db.Column(db.Integer, nullable=False, default=0)
    failures = db.Column(db.Integer, nullable=False, default=0)

    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    latency_ms = db.Column(db.Integer, nullable=False, default=0)
    estimated_cost_usd = db.Column(db.Float, nullable=False, default=0.0)

    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    run = db.relationship("PipelineRun", back_populates="stage_stats")

    __table_args__ = (
        db.UniqueConstraint("run_uuid", "stage", name="uq_stage_per_run"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "sequence": self.sequence,
            "upstream_stage": self.upstream_stage,
            "items_in": self.items_in,
            "items_out": self.items_out,
            "calls": self.calls,
            "retries": self.retries,
            "failures": self.failures,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "estimated_cost_usd": self.estimated_cost_usd,
            "last_error": self.last_error,
        }
