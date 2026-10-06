"""Pipeline run: one execution of Agent 1 -> Agent 2 -> Agent 3."""

from __future__ import annotations

from typing import Any

from app.constants import RUN_KIND_PIPELINE, RUN_STATUS_RUNNING
from app.extensions import db
from app.models.base import TimestampMixin, UUIDMixin, iso


class PipelineRun(UUIDMixin, TimestampMixin, db.Model):
    __tablename__ = "pipeline_runs"

    profile_uuid = db.Column(
        db.String(36),
        db.ForeignKey("business_profiles.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status = db.Column(db.String(32), nullable=False, default=RUN_STATUS_RUNNING)
    # "pipeline" is a full run. "recheck" is Agent 2 on one query. Same table so
    # both show up in cost and failure queries.
    kind = db.Column(db.String(16), nullable=False, default=RUN_KIND_PIPELINE, server_default=RUN_KIND_PIPELINE)
    queries_discovered = db.Column(db.Integer, nullable=False, default=0)
    queries_scored = db.Column(db.Integer, nullable=False, default=0)
    recommendations_generated = db.Column(db.Integer, nullable=False, default=0)

    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    # Rollups of llm_call_logs, kept on the run so a one-table query is enough
    # for the common "what did this run cost, and did it retry" question.
    llm_calls = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    llm_retries = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    llm_failures = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    estimated_cost_usd = db.Column(db.Float, nullable=False, default=0.0, server_default="0")

    error_message = db.Column(db.Text, nullable=True)
    # Non-fatal problems (a single query that failed to score, a DataForSEO timeout)
    # are kept here so a partially successful run stays diagnosable.
    warnings = db.Column(db.JSON, nullable=False, default=list)

    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    profile = db.relationship("BusinessProfile", back_populates="runs")
    queries = db.relationship("DiscoveredQuery", back_populates="run", lazy="dynamic")
    call_logs = db.relationship(
        "LlmCallLog",
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    stage_stats = db.relationship(
        "PipelineStageStat",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PipelineStageStat.sequence",
        lazy="selectin",
    )

    @property
    def tokens_used(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_uuid": self.uuid,
            "profile_uuid": self.profile_uuid,
            "status": self.status,
            "kind": self.kind,
            "queries_discovered": self.queries_discovered,
            "queries_scored": self.queries_scored,
            "recommendations_generated": self.recommendations_generated,
            "tokens_used": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "total": self.tokens_used,
            },
            "llm_calls": self.llm_calls,
            "llm_retries": self.llm_retries,
            "llm_failures": self.llm_failures,
            "estimated_cost_usd": self.estimated_cost_usd,
            "stages": [stage.to_dict() for stage in self.stage_stats],
            "error_message": self.error_message,
            "warnings": self.warnings or [],
            "started_at": iso(self.started_at),
            "completed_at": iso(self.completed_at),
        }

    def __repr__(self) -> str:
        return f"<PipelineRun {self.uuid} {self.status}>"
