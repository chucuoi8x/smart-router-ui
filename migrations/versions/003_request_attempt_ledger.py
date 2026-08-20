"""Add request and attempt ledger tables.

Revision ID: 003_request_attempt_ledger
Revises: 002_usage_ledger_provenance
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "003_request_attempt_ledger"
down_revision = "002_usage_ledger_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_ledger",
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("route_id", sa.String(length=128), nullable=False),
        sa.Column("logical_model", sa.String(length=255), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(op.f("ix_request_ledger_logical_model"), "request_ledger", ["logical_model"], unique=False)
    op.create_index(op.f("ix_request_ledger_route_id"), "request_ledger", ["route_id"], unique=False)

    op.create_table(
        "attempt_ledger",
        sa.Column("attempt_id", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index(op.f("ix_attempt_ledger_provider_id"), "attempt_ledger", ["provider_id"], unique=False)
    op.create_index(op.f("ix_attempt_ledger_request_id"), "attempt_ledger", ["request_id"], unique=False)
    op.create_index(op.f("ix_attempt_ledger_status"), "attempt_ledger", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_attempt_ledger_status"), table_name="attempt_ledger")
    op.drop_index(op.f("ix_attempt_ledger_request_id"), table_name="attempt_ledger")
    op.drop_index(op.f("ix_attempt_ledger_provider_id"), table_name="attempt_ledger")
    op.drop_table("attempt_ledger")

    op.drop_index(op.f("ix_request_ledger_route_id"), table_name="request_ledger")
    op.drop_index(op.f("ix_request_ledger_logical_model"), table_name="request_ledger")
    op.drop_table("request_ledger")
