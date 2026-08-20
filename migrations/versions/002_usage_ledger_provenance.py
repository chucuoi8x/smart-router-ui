"""Add usage ledger provenance columns.

Revision ID: 002_usage_ledger_provenance
Revises: 001_initial_baseline
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "002_usage_ledger_provenance"
down_revision = "001_initial_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_ledger", sa.Column("attempt_id", sa.String(length=128), nullable=False))
    op.add_column("usage_ledger", sa.Column("credential_id", sa.String(length=64), nullable=True))
    op.add_column("usage_ledger", sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("usage_ledger", sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("usage_ledger", sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("usage_ledger", sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("usage_ledger", sa.Column("native_metric", sa.String(length=64), nullable=True))
    op.add_column("usage_ledger", sa.Column("native_amount", sa.Float(), nullable=True))
    op.add_column("usage_ledger", sa.Column("currency", sa.String(length=16), nullable=True))
    op.add_column("usage_ledger", sa.Column("source", sa.String(length=64), nullable=False, server_default="generic_estimate"))
    op.add_column("usage_ledger", sa.Column("confidence", sa.String(length=64), nullable=False, server_default="estimated"))
    op.add_column("usage_ledger", sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index(op.f("ix_usage_ledger_attempt_id"), "usage_ledger", ["attempt_id"], unique=False)
    op.create_index(op.f("ix_usage_ledger_credential_id"), "usage_ledger", ["credential_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_usage_ledger_credential_id"), table_name="usage_ledger")
    op.drop_index(op.f("ix_usage_ledger_attempt_id"), table_name="usage_ledger")
    op.drop_column("usage_ledger", "estimated")
    op.drop_column("usage_ledger", "confidence")
    op.drop_column("usage_ledger", "source")
    op.drop_column("usage_ledger", "currency")
    op.drop_column("usage_ledger", "native_amount")
    op.drop_column("usage_ledger", "native_metric")
    op.drop_column("usage_ledger", "total_tokens")
    op.drop_column("usage_ledger", "reasoning_tokens")
    op.drop_column("usage_ledger", "cache_write_tokens")
    op.drop_column("usage_ledger", "cached_input_tokens")
    op.drop_column("usage_ledger", "credential_id")
    op.drop_column("usage_ledger", "attempt_id")
