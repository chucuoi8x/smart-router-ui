"""Provider registry: credentials and imported models tables.

PR-05 persists the Control Plane provider registry. ``provider_connections``
already exists from the initial baseline; this revision adds the two tables the
in-memory admin dictionaries used to hold: per-connection credentials and
discovered/imported models.

Revision ID: 005_provider_registry
Revises: 004_quota_resources
Create Date: 2026-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "005_provider_registry"
down_revision = "004_quota_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("credential_encrypted", sa.String(length=4096), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["provider_connections.id"],
            name=op.f("fk_provider_credentials_connection_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_provider_credentials_connection_id"),
        "provider_credentials",
        ["connection_id"],
        unique=False,
    )

    op.create_table(
        "provider_models",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["provider_connections.id"],
            name=op.f("fk_provider_models_connection_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "model_id", name="uq_provider_models_connection_model"),
    )
    op.create_index(
        op.f("ix_provider_models_connection_id"),
        "provider_models",
        ["connection_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_provider_models_connection_id"), table_name="provider_models")
    op.drop_table("provider_models")
    op.drop_index(op.f("ix_provider_credentials_connection_id"), table_name="provider_credentials")
    op.drop_table("provider_credentials")
