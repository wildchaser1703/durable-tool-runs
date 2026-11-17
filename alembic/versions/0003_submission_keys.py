"""Deduplicate client submissions without changing existing runs."""

import sqlalchemy as sa

from alembic import op

revision = "0003_submission_keys"
down_revision = "0002_claim_fencing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("request_key", sa.String(128)))
    op.add_column("runs", sa.Column("request_hash", sa.String(64)))
    op.create_unique_constraint("uq_runs_request_key", "runs", ["request_key"])


def downgrade():
    op.drop_constraint("uq_runs_request_key", "runs", type_="unique")
    op.drop_column("runs", "request_hash")
    op.drop_column("runs", "request_key")
