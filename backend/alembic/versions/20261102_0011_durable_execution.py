"""Track 1: durable execution - campaign leases and resume counters.

* campaigns.lease_owner / lease_expires_at: the worker running a campaign renews a short lease;
  an expired lease on a RUNNING campaign means its worker died, and the resume sweep restarts
  it from its checkpoint (pages already fetched are skipped, unextracted pages re-queued).
* campaigns.resume_count / applications.resume_count: automatic resumes are capped; past the
  cap the run is handed back to the user as before.

Revision ID: 0011_durable_execution
Revises: 0010_agent_training_and_dom_rag
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_durable_execution"
down_revision: str | None = "0010_agent_training_and_dom_rag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("lease_owner", sa.String(120), nullable=True))
    op.add_column(
        "campaigns", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "campaigns",
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "applications",
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("applications", "resume_count")
    op.drop_column("campaigns", "resume_count")
    op.drop_column("campaigns", "lease_expires_at")
    op.drop_column("campaigns", "lease_owner")
