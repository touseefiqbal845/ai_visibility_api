"""Request validation.

Marshmallow rather than Pydantic purely because it is already the idiomatic pairing
with Flask-SQLAlchemy in this stack; either would do the job.
"""

from __future__ import annotations

import re

from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.constants import VISIBILITY_NOT_VISIBLE, VISIBILITY_UNKNOWN, VISIBILITY_VISIBLE

_DOMAIN_RE = re.compile(
    r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.[a-z0-9-]{1,63})*\.[a-z]{2,}$", re.IGNORECASE
)


def _normalise_domain(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = re.sub(r"^www\.", "", cleaned)
    return cleaned.rstrip("/").split("/")[0]


class DomainField(fields.String):
    """Accepts a bare domain or a pasted URL and stores the bare domain."""

    def _deserialize(self, value, attr, data, **kwargs):
        raw = super()._deserialize(value, attr, data, **kwargs)
        cleaned = _normalise_domain(raw)
        if not _DOMAIN_RE.match(cleaned):
            raise ValidationError(f"{raw!r} is not a valid domain")
        return cleaned


class ProfileCreateSchema(Schema):
    name = fields.String(required=True, validate=validate.Length(min=1, max=255))
    domain = DomainField(required=True)
    industry = fields.String(required=True, validate=validate.Length(min=1, max=255))
    description = fields.String(load_default=None, allow_none=True, validate=validate.Length(max=2000))
    competitors = fields.List(
        DomainField(),
        load_default=list,
        validate=validate.Length(max=20),
    )

    @validates_schema
    def _no_self_competition(self, data, **kwargs):
        domain = data.get("domain")
        competitors = data.get("competitors") or []
        if domain and domain in competitors:
            raise ValidationError(
                "a profile cannot list its own domain as a competitor", field_name="competitors"
            )
        if len(set(competitors)) != len(competitors):
            raise ValidationError("competitors must be unique", field_name="competitors")


class QueryFilterSchema(Schema):
    min_score = fields.Float(load_default=None, validate=validate.Range(min=0.0, max=1.0))
    status = fields.String(
        load_default=None,
        validate=validate.OneOf([VISIBILITY_VISIBLE, VISIBILITY_NOT_VISIBLE, VISIBILITY_UNKNOWN]),
    )
    page = fields.Integer(load_default=1, validate=validate.Range(min=1))
    per_page = fields.Integer(load_default=20, validate=validate.Range(min=1, max=100))


class RecommendationFilterSchema(Schema):
    priority = fields.String(
        load_default=None, validate=validate.OneOf(["high", "medium", "low"])
    )
    page = fields.Integer(load_default=1, validate=validate.Range(min=1))
    per_page = fields.Integer(load_default=20, validate=validate.Range(min=1, max=100))
