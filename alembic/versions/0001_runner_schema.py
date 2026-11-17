"""Create durable runner state.

Revision ID: 0001_runner_schema
Revises:
Create Date: 2026-11-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_runner_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The first public prototype used metadata.create_all(). Treat that matching
    # development schema as the migration baseline so existing local demos can
    # adopt Alembic without dropping their run history. Clean installs create
    # the schema below; later revisions can evolve it normally.
    if "runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_table(
        "steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=80), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("run_id", "position", name="uq_step_position_per_run"),
    )
    op.create_index("ix_steps_run_id", "steps", ["run_id"])
    op.create_index("ix_steps_status", "steps", ["status"])
    op.create_index("ix_steps_lease_expires_at", "steps", ["lease_expires_at"])
    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("step_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["steps.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_steps_lease_expires_at", table_name="steps")
    op.drop_index("ix_steps_status", table_name="steps")
    op.drop_index("ix_steps_run_id", table_name="steps")
    op.drop_table("steps")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_table("runs")
