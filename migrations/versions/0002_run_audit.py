"""run audit: llm call logs and stage stats

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "pipeline_runs",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="pipeline"),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("llm_retries", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("llm_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
    )

    op.create_table(
        "llm_call_logs",
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("run_uuid", sa.String(length=36), nullable=False),
        sa.Column("profile_uuid", sa.String(length=36), nullable=False),
        sa.Column("query_uuid", sa.String(length=36), nullable=True),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("is_retry", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_uuid"], ["business_profiles.uuid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["query_uuid"], ["discovered_queries.uuid"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_uuid"], ["pipeline_runs.uuid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("uuid"),
    )
    op.create_index("ix_llm_call_logs_run_uuid", "llm_call_logs", ["run_uuid"])
    op.create_index("ix_llm_call_logs_profile_uuid", "llm_call_logs", ["profile_uuid"])
    op.create_index("ix_llm_call_logs_query_uuid", "llm_call_logs", ["query_uuid"])
    op.create_index("ix_llm_call_logs_agent", "llm_call_logs", ["agent"])
    op.create_index("ix_llm_call_logs_status", "llm_call_logs", ["status"])

    op.create_table(
        "pipeline_stage_stats",
        sa.Column("uuid", sa.String(length=36), nullable=False),
        sa.Column("run_uuid", sa.String(length=36), nullable=False),
        sa.Column("profile_uuid", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("upstream_stage", sa.String(length=64), nullable=True),
        sa.Column("items_in", sa.Integer(), nullable=False),
        sa.Column("items_out", sa.Integer(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_uuid"], ["business_profiles.uuid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_uuid"], ["pipeline_runs.uuid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("uuid"),
        sa.UniqueConstraint("run_uuid", "stage", name="uq_stage_per_run"),
    )
    op.create_index("ix_pipeline_stage_stats_run_uuid", "pipeline_stage_stats", ["run_uuid"])
    op.create_index("ix_pipeline_stage_stats_profile_uuid", "pipeline_stage_stats", ["profile_uuid"])
    op.create_index("ix_pipeline_stage_stats_stage", "pipeline_stage_stats", ["stage"])


def downgrade():
    op.drop_table("pipeline_stage_stats")
    op.drop_table("llm_call_logs")
    op.drop_column("pipeline_runs", "estimated_cost_usd")
    op.drop_column("pipeline_runs", "llm_failures")
    op.drop_column("pipeline_runs", "llm_retries")
    op.drop_column("pipeline_runs", "llm_calls")
    op.drop_column("pipeline_runs", "kind")
