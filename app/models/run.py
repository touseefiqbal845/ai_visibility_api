"""Pipeline run: one execution of Agent 1 -> Agent 2 -> Agent 3."""

from __future__ import annotations

from typing import Any

from app.constants import RUN_STATUS_RUNNING
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
    queries_discovered = db.Column(db.Integer, nullable=False, default=0)
    queries_scored = db.Column(db.Integer, nullable=False, default=0)
    recommendations_generated = db.Column(db.Integer, nullable=False, default=0)

    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)

    error_message = db.Column(db.Text, nullable=True)
    # Non-fatal problems (a single query that failed to score, a DataForSEO timeout)
    # are kept here so a partially successful run stays diagnosable.
    warnings = db.Column(db.JSON, nullable=False, default=list)

    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    profile = db.relationship("BusinessProfile", back_populates="runs")
    queries = db.relationship("DiscoveredQuery", back_populates="run", lazy="dynamic")

    @property
    def tokens_used(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_uuid": self.uuid,
            "profile_uuid": self.profile_uuid,
            "status": self.status,
            "queries_discovered": self.queries_discovered,
            "queries_scored": self.queries_scored,
            "recommendations_generated": self.recommendations_generated,
            "tokens_used": {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "total": self.tokens_used,
            },
            "error_message": self.error_message,
            "warnings": self.warnings or [],
            "started_at": iso(self.started_at),
            "completed_at": iso(self.completed_at),
        }

    def __repr__(self) -> str:
        return f"<PipelineRun {self.uuid} {self.status}>"
