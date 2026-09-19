from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, JSON, String, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProviderConnection(Base):
    __tablename__ = "provider_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("conn"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    driver: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Deprecated compatibility column. New credentials live in ProviderCredential.
    credential_encrypted: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("cred"))
    connection_id: Mapped[str] = mapped_column(String(64), ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_encrypted: Mapped[str] = mapped_column(String(4096), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class ProviderModel(Base):
    __tablename__ = "provider_models"
    __table_args__ = (UniqueConstraint("connection_id", "model_id", name="uq_provider_models_connection_model"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("pmod"))
    connection_id: Mapped[str] = mapped_column(String(64), ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class ConfigRevision(Base):
    __tablename__ = "config_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("rev"))
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RequestLedger(Base):
    __tablename__ = "request_ledger"
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    route_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    logical_model: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class AttemptLedger(Base):
    __tablename__ = "attempt_ledger"
    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("led"))
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    credential_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    native_metric: Mapped[str | None] = mapped_column(String(64), nullable=True)
    native_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="generic_estimate")
    confidence: Mapped[str] = mapped_column(String(64), nullable=False, default="estimated")
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("proj"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    secret_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class ProjectKey(Base):
    __tablename__ = "project_keys"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("key"))
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class ProjectBudget(Base):
    __tablename__ = "project_budgets"
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USD")
    ceiling: Mapped[float] = mapped_column(Float, nullable=False)
    used: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    allow_paid_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)


class Policy(Base):
    __tablename__ = "policies"
    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_budget: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.id"), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _new_id("alert"))
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(2048), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class ControlSetting(Base):
    __tablename__ = "control_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(1024), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)



class QuotaResourceState(Base):
    __tablename__ = "quota_resources"
    resource_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    limit: Mapped[int] = mapped_column(Integer, nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    safety_buffer: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hard_limit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="configured")
    confidence: Mapped[str] = mapped_column(String(64), nullable=False, default="high")
    shared_group_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    window_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)
