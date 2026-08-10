"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "business_profiles",
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("industry", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("competitors", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index("ix_business_profiles_domain", "business_profiles", ["domain"])

    op.create_table(
        "pipeline_runs",
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("profile_uuid", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("queries_discovered", sa.Integer(), nullable=False),
        sa.Column("queries_scored", sa.Integer(), nullable=False),
        sa.Column("recommendations_generated", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_uuid"], ["business_profiles.uuid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index("ix_pipeline_runs_profile_uuid", "pipeline_runs", ["profile_uuid"])

    op.create_table(
        "discovered_queries",
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("profile_uuid", sa.String(length=36), nullable=False),
        sa.Column("run_uuid", sa.String(length=36), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("commercial_intent", sa.String(length=32), nullable=False),
        sa.Column("estimated_search_volume", sa.Integer(), nullable=True),
        sa.Column("competitive_difficulty", sa.Integer(), nullable=True),
        sa.Column("opportunity_score", sa.Float(), nullable=True),
        sa.Column("visibility_status", sa.String(length=32), nullable=False),
        sa.Column("domain_visible", sa.Boolean(), nullable=True),
        sa.Column("visibility_position", sa.Integer(), nullable=True),
        sa.Column("competitors_visible", sa.JSON(), nullable=False),
        sa.Column("answer_excerpt", sa.Text(), nullable=True),
        sa.Column("scoring_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_uuid"], ["business_profiles.uuid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_uuid"], ["pipeline_runs.uuid"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("uuid"),
        sa.UniqueConstraint("profile_uuid", "query_text", name="uq_query_per_profile"),
    )
    op.create_index("ix_discovered_queries_profile_uuid", "discovered_queries", ["profile_uuid"])
    op.create_index("ix_discovered_queries_run_uuid", "discovered_queries", ["run_uuid"])
    op.create_index("ix_discovered_queries_opportunity_score", "discovered_queries", ["opportunity_score"])
    op.create_index("ix_query_profile_score", "discovered_queries", ["profile_uuid", "opportunity_score"])

    op.create_table(
        "content_recommendations",
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("profile_uuid", sa.String(length=36), nullable=False),
        sa.Column("query_uuid", sa.String(length=36), nullable=False),
        sa.Column("run_uuid", sa.String(length=36), nullable=True),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("target_keywords", sa.JSON(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_uuid"], ["business_profiles.uuid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["query_uuid"], ["discovered_queries.uuid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_uuid"], ["pipeline_runs.uuid"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index("ix_content_recommendations_profile_uuid", "content_recommendations", ["profile_uuid"])
    op.create_index("ix_content_recommendations_query_uuid", "content_recommendations", ["query_uuid"])
    op.create_index("ix_content_recommendations_run_uuid", "content_recommendations", ["run_uuid"])


def downgrade():
    op.drop_table("content_recommendations")
    op.drop_table("discovered_queries")
    op.drop_table("pipeline_runs")
    op.drop_table("business_profiles")
