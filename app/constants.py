"""Domain vocabulary shared by models, agents and the scoring formula.

Kept out of the models package so the scoring logic and the agents can be imported -
and tested - without pulling in SQLAlchemy.
"""

from __future__ import annotations

# Profile lifecycle
PROFILE_STATUS_CREATED = "created"
PROFILE_STATUS_RUNNING = "running"
PROFILE_STATUS_READY = "ready"
PROFILE_STATUS_FAILED = "failed"

# Run lifecycle. "completed_with_errors" is deliberately distinct from both of its
# neighbours: the run produced usable output but something needs looking at.
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_PARTIAL = "completed_with_errors"
RUN_STATUS_FAILED = "failed"

# A full pipeline and a one-query recheck share pipeline_runs so both are queryable,
# and `kind` keeps the two from being mixed in an aggregate.
RUN_KIND_PIPELINE = "pipeline"
RUN_KIND_RECHECK = "recheck"

# Stages in execution order. Agent names match the agent classes so a call log and
# a stage row join on the same string. DataForSEO is a stage, not an agent.
STAGE_DISCOVERY = "query_discovery"
STAGE_METRICS = "dataforseo"
STAGE_SCORING = "visibility_scoring"
STAGE_RECOMMENDATION = "content_recommendation"
STAGE_SEQUENCE = {
    STAGE_DISCOVERY: 1,
    STAGE_METRICS: 2,
    STAGE_SCORING: 3,
    STAGE_RECOMMENDATION: 4,
}
STAGE_UPSTREAM = {
    STAGE_DISCOVERY: None,
    STAGE_METRICS: STAGE_DISCOVERY,
    STAGE_SCORING: STAGE_METRICS,
    STAGE_RECOMMENDATION: STAGE_SCORING,
}

# Outcome of one model call. "unparseable" is the corrective retry, not a failure:
# the call returned, the body was not JSON, and the agent tried once more.
LLM_STATUS_SUCCESS = "success"
LLM_STATUS_UNPARSEABLE = "unparseable"
LLM_STATUS_ERROR = "error"

# Visibility
VISIBILITY_VISIBLE = "visible"
VISIBILITY_NOT_VISIBLE = "not_visible"
VISIBILITY_UNKNOWN = "unknown"
VISIBILITY_STATUSES = (VISIBILITY_VISIBLE, VISIBILITY_NOT_VISIBLE, VISIBILITY_UNKNOWN)

# Commercial intent
INTENT_COMPARISON = "comparison"
INTENT_COMMERCIAL = "commercial"
INTENT_INFORMATIONAL = "informational"
INTENTS = (INTENT_COMPARISON, INTENT_COMMERCIAL, INTENT_INFORMATIONAL)

# Recommendations
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)

CONTENT_TYPES = (
    "blog_post",
    "landing_page",
    "comparison_page",
    "faq",
    "case_study",
    "documentation",
)
DEFAULT_CONTENT_TYPE = "blog_post"
