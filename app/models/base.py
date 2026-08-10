"""Shared column mixins.

Primary keys are UUID strings rather than autoincrement integers: profile and query
identifiers are handed out over the API, and sequential integers leak volume and let
one tenant enumerate another's records.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.extensions import db


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDMixin:
    uuid = db.Column(db.String(36), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


def iso(value: datetime | None) -> str | None:
    """Serialise a datetime as UTC ISO-8601 with a trailing Z.

    SQLite hands datetimes back without tzinfo, so naive values are assumed UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
