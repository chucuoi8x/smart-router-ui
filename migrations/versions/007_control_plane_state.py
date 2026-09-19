"""P0-04 durable Control Plane state migration."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "007_control_plane_state"
down_revision = "006_quota_schema_correctness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("secret_key_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "project_keys",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("secret_encrypted", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_project_keys_project_id", "project_keys", ["project_id"])
    op.create_table(
        "project_budgets",
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("currency", sa.String(length=16), nullable=False, server_default="USD"),
        sa.Column("ceiling", sa.Float(), nullable=False),
        sa.Column("used", sa.Float(), nullable=False, server_default="0"),
        sa.Column("allow_paid_fallback", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "policies",
        sa.Column("policy_id", sa.String(length=64), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("requires_budget", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("project_id", sa.String(length=64), sa.ForeignKey("projects.id"), nullable=True),
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("message", sa.String(length=2048), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_alerts_severity", "alerts", ["severity"])
    op.create_index("ix_alerts_status", "alerts", ["status"])
    op.create_table(
        "control_settings",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("value", sa.String(length=1024), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("control_settings")
    op.drop_index("ix_alerts_status", table_name="alerts")
    op.drop_index("ix_alerts_severity", table_name="alerts")
    op.drop_table("alerts")
    op.drop_table("policies")
    op.drop_table("project_budgets")
    op.drop_index("ix_project_keys_project_id", table_name="project_keys")
    op.drop_table("project_keys")
    op.drop_table("projects")
