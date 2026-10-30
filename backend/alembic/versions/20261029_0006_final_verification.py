"""Milestone 5: final verification gate (human OK bound to a preview hash) and submission receipts.

Revision ID: 0006_final_verification
Revises: 0005_discovery_streaming
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

from app.infrastructure.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0006_final_verification"
down_revision: str | None = "0005_discovery_streaming"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "final_verifications",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "checkpoint_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("application_checkpoints.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("preview_hash_seen", sa.String(64), nullable=False),
        sa.Column("approved_preview_hash", sa.String(64), nullable=True),
        sa.Column("observed_hash", sa.String(64), nullable=True),
        sa.Column("edited_fields", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("mismatches", pg.JSONB, nullable=False, server_default="[]"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_url", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        # Receipt screenshot (object storage, encrypted) and confirmation text (encrypted: it
        # usually contains the applicant's name / reference numbers).
        sa.Column("receipt_key", sa.Text, nullable=True),
        sa.Column("receipt_nonce", sa.LargeBinary, nullable=True),
        sa.Column("receipt_key_version", sa.SmallInteger, nullable=True),
        sa.Column("receipt_text_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("receipt_text_nonce", sa.LargeBinary, nullable=True),
        sa.Column("receipt_text_key_version", sa.SmallInteger, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_final_verifications_user_app",
        "final_verifications",
        ["user_id", "application_id", "created_at"],
    )
    enable_tenant_rls(op, "final_verifications")
    op.create_index(
        "ix_applications_lease",
        "applications",
        ["lease_expires_at"],
        postgresql_where=sa.text("lease_expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_applications_lease", table_name="applications")
    disable_tenant_rls(op, "final_verifications")
    op.drop_table("final_verifications")
