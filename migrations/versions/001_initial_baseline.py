"""Initial database baseline.

Revision ID: 001_initial_baseline
Revises:
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "001_initial_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("driver", sa.String(length=128), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("credential_encrypted", sa.String(length=4096), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_provider_connections_template_id"),
        "provider_connections",
        ["template_id"],
        unique=False,
    )

    op.create_table(
        "config_revisions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_data", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_config_revisions_is_active"),
        "config_revisions",
        ["is_active"],
        unique=False,
    )

    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_usage_ledger_provider_id"),
        "usage_ledger",
        ["provider_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_usage_ledger_request_id"),
        "usage_ledger",
        ["request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_usage_ledger_request_id"), table_name="usage_ledger")
    op.drop_index(op.f("ix_usage_ledger_provider_id"), table_name="usage_ledger")
    op.drop_table("usage_ledger")

    op.drop_index(op.f("ix_config_revisions_is_active"), table_name="config_revisions")
    op.drop_table("config_revisions")

    op.drop_index(op.f("ix_provider_connections_template_id"), table_name="provider_connections")
    op.drop_table("provider_connections")
