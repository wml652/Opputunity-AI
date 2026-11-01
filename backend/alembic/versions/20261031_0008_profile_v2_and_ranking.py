"""Milestone 8: student profile v2 and opportunity ranking.

* student_profiles.details: profile v2 non-PII facts (semester/year, CGPA %, secondary school,
  English proficiency / MOI, standardized tests, publications, research interests, work mode,
  start date, visa / passport / financial-need / first-generation flags). The new personal
  fields (city, postal code, links, emergency contact, referees) live in the existing encrypted
  PII document - no plaintext column.
* Ranking (`match_score`, `rank_breakdown`) is computed deterministically on read from the
  opportunity, its latest verdict and the current profile, so it always reflects profile edits;
  nothing is stored for it.

Revision ID: 0008_profile_v2_and_ranking
Revises: 0007_rag_and_batch_apply
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0008_profile_v2_and_ranking"
down_revision: str | None = "0007_rag_and_batch_apply"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "student_profiles",
        sa.Column("details", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("student_profiles", "details")
