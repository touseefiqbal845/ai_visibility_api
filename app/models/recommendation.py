"""Content recommendation produced by Agent 3."""

from __future__ import annotations

from typing import Any

from app.constants import PRIORITY_MEDIUM
from app.extensions import db
from app.models.base import TimestampMixin, UUIDMixin, iso


class ContentRecommendation(UUIDMixin, TimestampMixin, db.Model):
    __tablename__ = "content_recommendations"

    profile_uuid = db.Column(
        db.String(36),
        db.ForeignKey("business_profiles.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query_uuid = db.Column(
        db.String(36),
        db.ForeignKey("discovered_queries.uuid", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_uuid = db.Column(
        db.String(36),
        db.ForeignKey("pipeline_runs.uuid", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    content_type = db.Column(db.String(64), nullable=False)
    title = db.Column(db.String(500), nullable=False)
    rationale = db.Column(db.Text, nullable=False)
    target_keywords = db.Column(db.JSON, nullable=False, default=list)
    priority = db.Column(db.String(16), nullable=False, default=PRIORITY_MEDIUM)

    
    profile = db.relationship("BusinessProfile", back_populates="recommendations")
    # Named discovered_query, not query: a relationship called "query" shadows
    # Flask-SQLAlchemy's Model.query and breaks every class-level lookup.
    discovered_query = db.relationship("DiscoveredQuery", back_populates="recommendations")

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation_uuid": self.uuid,
            "profile_uuid": self.profile_uuid,
            "target_query_uuid": self.query_uuid,
            "target_query_text": self.discovered_query.query_text if self.discovered_query else None,
            "run_uuid": self.run_uuid,
            "content_type": self.content_type,
            "title": self.title,
            "rationale": self.rationale,
            "target_keywords": self.target_keywords or [],
            "priority": self.priority,
            "created_at": iso(self.created_at),
        }

    def __repr__(self) -> str:
        return f"<ContentRecommendation {self.title[:40]!r}>"

