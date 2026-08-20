"""Add quota resource state table.

Revision ID: 004_quota_resources
Revises: 003_request_attempt_ledger
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "004_quota_resources"
down_revision = "003_request_attempt_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quota_resources",
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("limit", sa.Integer(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("safety_buffer", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("hard_limit", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="configured"),
        sa.Column("confidence", sa.String(length=64), nullable=False, server_default="high"),
        sa.Column("shared_group_id", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("resource_id"),
    )
    op.create_index(op.f("ix_quota_resources_metric"), "quota_resources", ["metric"], unique=False)
    op.create_index(op.f("ix_quota_resources_scope"), "quota_resources", ["scope"], unique=False)
    op.create_index(op.f("ix_quota_resources_shared_group_id"), "quota_resources", ["shared_group_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_quota_resources_shared_group_id"), table_name="quota_resources")
    op.drop_index(op.f("ix_quota_resources_scope"), table_name="quota_resources")
    op.drop_index(op.f("ix_quota_resources_metric"), table_name="quota_resources")
    op.drop_table("quota_resources")
