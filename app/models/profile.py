"""Business profile: the tenant record that everything else hangs off."""

from __future__ import annotations

from typing import Any

from app.constants import PROFILE_STATUS_CREATED
from app.extensions import db
from app.models.base import TimestampMixin, UUIDMixin, iso


class BusinessProfile(UUIDMixin, TimestampMixin, db.Model):
    __tablename__ = "business_profiles"

    name = db.Column(db.String(255), nullable=False)
    domain = db.Column(db.String(255), nullable=False, index=True)
    industry = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)

    # Competitors are a flat list of domains with no attributes of their own and no
    # cross-profile identity, so a JSON column beats a join table here. If we later
    # need per-competitor visibility history this becomes a real table.
    competitors = db.Column(db.JSON, nullable=False, default=list)

    status = db.Column(db.String(32), nullable=False, default=PROFILE_STATUS_CREATED)

    runs = db.relationship(
        "PipelineRun",
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    queries = db.relationship(
        "DiscoveredQuery",
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    recommendations = db.relationship(
        "ContentRecommendation",
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_uuid": self.uuid,
            "name": self.name,
            "domain": self.domain,
            "industry": self.industry,
            "description": self.description,
            "competitors": self.competitors or [],
            "status": self.status,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }

    def __repr__(self) -> str:
        return f"<BusinessProfile {self.domain}>"
