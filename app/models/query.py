"""A query discovered by Agent 1 and scored by Agent 2."""

from __future__ import annotations

from typing import Any

from app.constants import INTENT_INFORMATIONAL, VISIBILITY_UNKNOWN
from app.extensions import db
from app.models.base import TimestampMixin, UUIDMixin, iso


class DiscoveredQuery(UUIDMixin, TimestampMixin, db.Model):
    __tablename__ = "discovered_queries"

    profile_uuid = db.Column(
        db.String(36),
        db.ForeignKey("business_profiles.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Kept nullable so a re-check never orphans a query if its original run is purged.
    run_uuid = db.Column(
        db.String(36),
        db.ForeignKey("pipeline_runs.uuid", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    query_text = db.Column(db.Text, nullable=False)
    commercial_intent = db.Column(db.String(32), nullable=False, default=INTENT_INFORMATIONAL)

    # Nullable on purpose: these come from DataForSEO, and when the provider has no
    # data for a keyword we store null rather than inventing a number.
    estimated_search_volume = db.Column(db.Integer, nullable=True)
    competitive_difficulty = db.Column(db.Integer, nullable=True)

    opportunity_score = db.Column(db.Float, nullable=True, index=True)

    visibility_status = db.Column(db.String(32), nullable=False, default=VISIBILITY_UNKNOWN)
    domain_visible = db.Column(db.Boolean, nullable=True)
    visibility_position = db.Column(db.Integer, nullable=True)
    competitors_visible = db.Column(db.JSON, nullable=False, default=list)

    # The raw assistant answer Agent 2 checked against. Worth persisting: it is the
    # evidence behind domain_visible, and re-checks are only meaningful as a diff.
    answer_excerpt = db.Column(db.Text, nullable=True)

    scoring_error = db.Column(db.Text, nullable=True)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    discovered_at = db.Column(db.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("profile_uuid", "query_text", name="uq_query_per_profile"),
        db.Index("ix_query_profile_score", "profile_uuid", "opportunity_score"),
    )

    profile = db.relationship("BusinessProfile", back_populates="queries")
    run = db.relationship("PipelineRun", back_populates="queries")
    recommendations = db.relationship(
        "ContentRecommendation",
        back_populates="discovered_query",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_uuid": self.uuid,
            "profile_uuid": self.profile_uuid,
            "run_uuid": self.run_uuid,
            "query_text": self.query_text,
            "commercial_intent": self.commercial_intent,
            "estimated_search_volume": self.estimated_search_volume,
            "competitive_difficulty": self.competitive_difficulty,
            "opportunity_score": self.opportunity_score,
            "visibility_status": self.visibility_status,
            "domain_visible": self.domain_visible,
            "visibility_position": self.visibility_position,
            "competitors_visible": self.competitors_visible or [],
            "scoring_error": self.scoring_error,
            "discovered_at": iso(self.discovered_at),
            "last_checked_at": iso(self.last_checked_at),
        }

    def __repr__(self) -> str:
        return f"<DiscoveredQuery {self.query_text[:40]!r}>"

