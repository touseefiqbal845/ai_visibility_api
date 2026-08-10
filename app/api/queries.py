"""Single-query operations."""

from __future__ import annotations

from flask import Blueprint, jsonify

from app.api.errors import NotFoundError, UpstreamError
from app.clients.dataforseo import DataForSEOError
from app.clients.llm import LLMError
from app.extensions import db
from app.models import DiscoveredQuery
from app.services.pipeline import PipelineError, PipelineOrchestrator

bp = Blueprint("queries", __name__, url_prefix="/api/v1/queries")


def _get_query(query_uuid: str) -> DiscoveredQuery:
    query = db.session.get(DiscoveredQuery, query_uuid)
    if query is None:
        raise NotFoundError(f"No query with uuid {query_uuid}")
    return query


@bp.get("/<query_uuid>")
def get_query(query_uuid: str):
    query = _get_query(query_uuid)
    body = query.to_dict()
    # Only exposed on the detail view: it is the evidence behind domain_visible, but
    # it would bloat every row of the list endpoint.
    body["answer_excerpt"] = query.answer_excerpt
    return jsonify(body), 200


@bp.post("/<query_uuid>/recheck")
def recheck_query(query_uuid: str):
    query = _get_query(query_uuid)

    previous = {
        "domain_visible": query.domain_visible,
        "visibility_position": query.visibility_position,
        "opportunity_score": query.opportunity_score,
    }

    try:
        orchestrator = PipelineOrchestrator.from_app()
        orchestrator.recheck(query)
    except (LLMError, DataForSEOError) as exc:
        raise UpstreamError(f"Pipeline dependencies are not configured: {exc}") from exc
    except PipelineError as exc:
        raise UpstreamError(str(exc)) from exc

    body = query.to_dict()
    body["previous"] = previous
    body["changed"] = {
        key: previous[key] != getattr(query, key)
        for key in ("domain_visible", "visibility_position", "opportunity_score")
    }
    return jsonify(body), 200
