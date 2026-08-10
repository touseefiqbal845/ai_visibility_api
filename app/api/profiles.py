"""Profile registration, pipeline trigger, and result retrieval."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func

from app.api.errors import NotFoundError, UpstreamError
from app.api.schemas import ProfileCreateSchema, QueryFilterSchema, RecommendationFilterSchema
from app.clients.dataforseo import DataForSEOError
from app.clients.llm import LLMError
from app.extensions import db, limiter
from app.models import (
    PROFILE_STATUS_CREATED,
    BusinessProfile,
    ContentRecommendation,
    DiscoveredQuery,
    PipelineRun,
)
from app.services.pipeline import PipelineOrchestrator

bp = Blueprint("profiles", __name__, url_prefix="/api/v1/profiles")


def _get_profile(profile_uuid: str) -> BusinessProfile:
    profile = db.session.get(BusinessProfile, profile_uuid)
    if profile is None:
        raise NotFoundError(f"No profile with uuid {profile_uuid}")
    return profile


def _paginate(query, page: int, per_page: int):
    total = query.order_by(None).count()
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    return items, {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page if total else 0,
    }


@bp.post("")
def create_profile():
    payload = ProfileCreateSchema().load(request.get_json(silent=True) or {})

    profile = BusinessProfile(
        name=payload["name"],
        domain=payload["domain"],
        industry=payload["industry"],
        description=payload.get("description"),
        competitors=payload.get("competitors") or [],
        status=PROFILE_STATUS_CREATED,
    )
    db.session.add(profile)
    db.session.commit()

    return jsonify(profile.to_dict()), 201


@bp.get("/<profile_uuid>")
def get_profile(profile_uuid: str):
    profile = _get_profile(profile_uuid)

    stats = db.session.query(
        func.count(DiscoveredQuery.uuid),
        func.avg(DiscoveredQuery.opportunity_score),
        func.sum(
            db.case((DiscoveredQuery.domain_visible.is_(True), 1), else_=0)
        ),
    ).filter(DiscoveredQuery.profile_uuid == profile.uuid).one()

    total_queries, avg_score, visible_count = stats
    total_queries = total_queries or 0
    visible_count = visible_count or 0

    last_run = (
        PipelineRun.query.filter_by(profile_uuid=profile.uuid)
        .order_by(PipelineRun.started_at.desc())
        .first()
    )

    body = profile.to_dict()
    body["stats"] = {
        "total_queries_discovered": total_queries,
        "average_opportunity_score": round(float(avg_score), 4) if avg_score is not None else None,
        "queries_visible": visible_count,
        "queries_not_visible": total_queries - visible_count,
        "visibility_rate": round(visible_count / total_queries, 4) if total_queries else None,
        "total_recommendations": ContentRecommendation.query.filter_by(
            profile_uuid=profile.uuid
        ).count(),
        "total_runs": PipelineRun.query.filter_by(profile_uuid=profile.uuid).count(),
    }
    body["last_run"] = last_run.to_dict() if last_run else None

    return jsonify(body), 200


@bp.post("/<profile_uuid>/run")
@limiter.limit(
    lambda: current_app.config["PIPELINE_RATE_LIMIT"],
    # Every call spends real credits at two providers, so the limit is per profile
    # rather than per client address.
    key_func=lambda: request.view_args.get("profile_uuid", "") if request.view_args else "",
    exempt_when=lambda: not current_app.config.get("RATELIMIT_ENABLED", True),
)
def run_pipeline(profile_uuid: str):
    profile = _get_profile(profile_uuid)

    try:
        orchestrator = PipelineOrchestrator.from_app()
    except (LLMError, DataForSEOError) as exc:
        raise UpstreamError(f"Pipeline dependencies are not configured: {exc}") from exc

    run = orchestrator.run(profile)

    top_queries = (
        DiscoveredQuery.query.filter_by(profile_uuid=profile.uuid)
        .filter(DiscoveredQuery.opportunity_score.isnot(None))
        .order_by(DiscoveredQuery.opportunity_score.desc())
        .limit(3)
        .all()
    )
    recommendations = ContentRecommendation.query.filter_by(run_uuid=run.uuid).all()

    body = run.to_dict()
    body["top_opportunities"] = [q.to_dict() for q in top_queries]
    body["recommendations"] = [r.to_dict() for r in recommendations]

    # 200 even for a failed run: the request itself succeeded and the body carries the
    # run's status and error. A 5xx here would imply the API broke, which it did not.
    return jsonify(body), 200


@bp.get("/<profile_uuid>/queries")
def list_queries(profile_uuid: str):
    profile = _get_profile(profile_uuid)
    filters = QueryFilterSchema().load(request.args.to_dict())

    query = DiscoveredQuery.query.filter_by(profile_uuid=profile.uuid)

    if filters["min_score"] is not None:
        query = query.filter(DiscoveredQuery.opportunity_score >= filters["min_score"])
    if filters["status"] is not None:
        query = query.filter(DiscoveredQuery.visibility_status == filters["status"])

    # Nulls last: unscored queries are not "the lowest opportunity", they are unknown.
    query = query.order_by(
        DiscoveredQuery.opportunity_score.is_(None),
        DiscoveredQuery.opportunity_score.desc(),
        DiscoveredQuery.discovered_at.desc(),
    )

    items, pagination = _paginate(query, filters["page"], filters["per_page"])
    return jsonify({"queries": [i.to_dict() for i in items], "pagination": pagination}), 200


@bp.get("/<profile_uuid>/recommendations")
def list_recommendations(profile_uuid: str):
    profile = _get_profile(profile_uuid)
    filters = RecommendationFilterSchema().load(request.args.to_dict())

    query = ContentRecommendation.query.filter_by(profile_uuid=profile.uuid)
    if filters["priority"] is not None:
        query = query.filter(ContentRecommendation.priority == filters["priority"])

    priority_rank = db.case(
        (ContentRecommendation.priority == "high", 0),
        (ContentRecommendation.priority == "medium", 1),
        else_=2,
    )
    query = query.order_by(priority_rank, ContentRecommendation.created_at.desc())

    items, pagination = _paginate(query, filters["page"], filters["per_page"])
    return jsonify(
        {"recommendations": [i.to_dict() for i in items], "pagination": pagination}
    ), 200


@bp.get("/<profile_uuid>/runs")
def list_runs(profile_uuid: str):
    profile = _get_profile(profile_uuid)
    runs = (
        PipelineRun.query.filter_by(profile_uuid=profile.uuid)
        .order_by(PipelineRun.started_at.desc())
        .all()
    )
    return jsonify({"runs": [r.to_dict() for r in runs]}), 200
