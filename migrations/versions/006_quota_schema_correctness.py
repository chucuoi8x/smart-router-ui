"""PR-08 quota schema migration.

Adds durable quota hierarchy and reset/window provenance fields.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "006_quota_schema_correctness"
down_revision = "005_provider_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quota_resources", sa.Column("parent_id", sa.String(length=128), nullable=True))
    op.add_column("quota_resources", sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("quota_resources", sa.Column("window_metadata", sa.JSON(), nullable=False, server_default="{}"))
    op.create_index(op.f("ix_quota_resources_parent_id"), "quota_resources", ["parent_id"], unique=False)
    op.create_index(op.f("ix_quota_resources_reset_at"), "quota_resources", ["reset_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_quota_resources_reset_at"), table_name="quota_resources")
    op.drop_index(op.f("ix_quota_resources_parent_id"), table_name="quota_resources")
    op.drop_column("quota_resources", "window_metadata")
    op.drop_column("quota_resources", "reset_at")
    op.drop_column("quota_resources", "parent_id")
