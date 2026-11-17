"""Fence every claim and persist retry availability.

Revision ID: 0002_claim_fencing
Revises: 0001_runner_schema
"""

import sqlalchemy as sa

from alembic import op
from app.schema_baseline import validate_baseline

revision = "0002_claim_fencing"
down_revision = "0001_runner_schema"
branch_labels = None
depends_on = None


def upgrade():
    # Also verifies databases stamped by the original permissive 0001 migration.
    validate_baseline(op.get_bind())
    op.add_column("steps", sa.Column("claim_token", sa.String(36), nullable=True))
    op.add_column(
        "steps",
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_steps_available_at", "steps", ["available_at"])


def downgrade():
    op.drop_index("ix_steps_available_at", table_name="steps")
    op.drop_column("steps", "available_at")
    op.drop_column("steps", "claim_token")
