"""PR-04 durable Control Plane state: projects, keys, budgets, policies, audit.

P0 invariant 3.4: these must not rely on process-local dictionaries.
All state lives in PostgreSQL/SQLite through this repository, so a restart
cannot lose project keys, budgets, policies, alerts, settings or audit trail.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from apps.gateway.db.models import (
    Alert,
    AuditEvent,
    ControlSetting,
    Policy,
    Project,
    ProjectBudget,
    ProjectKey,
)


class ControlPlaneRepository:
    """Async repository for durable Control Plane state."""

    def __init__(self, session: Any) -> None:
        self._session = session

    # ── projects ─────────────────────────────────────────────────────

    async def create_project(self, *, name: str, description: str, secret_key_hash: str) -> Project:
        project = Project(name=name, description=description, secret_key_hash=secret_key_hash)
        self._session.add(project)
        await self._session.flush()
        return project

    async def get_project(self, project_id: str) -> Project | None:
        return await self._session.get(Project, project_id)

    async def list_projects(self) -> list[Project]:
        rows = await self._session.scalars(select(Project).order_by(Project.created_at))
        return list(rows.all())

    async def delete_project(self, project_id: str) -> bool:
        project = await self.get_project(project_id)
        if project is None:
            return False
        await self._session.delete(project)
        await self._session.flush()
        return True

    # ── project keys ─────────────────────────────────────────────────

    async def create_project_key(
        self, *, project_id: str, alias: str, secret_encrypted: str
    ) -> ProjectKey:
        record = ProjectKey(
            project_id=project_id, alias=alias, secret_encrypted=secret_encrypted
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def list_project_keys(self, project_id: str) -> list[ProjectKey]:
        rows = await self._session.scalars(
            select(ProjectKey)
            .where(ProjectKey.project_id == project_id)
            .order_by(ProjectKey.created_at)
        )
        return list(rows.all())

    async def get_project_key(self, project_id: str, key_id: str) -> ProjectKey | None:
        rows = await self._session.scalars(
            select(ProjectKey).where(
                ProjectKey.id == key_id, ProjectKey.project_id == project_id
            )
        )
        return rows.first()

    async def revoke_project_key(self, record: ProjectKey) -> ProjectKey:
        record.is_active = False
        record.revoked_at = datetime.now(UTC)
        await self._session.flush()
        return record

    # ── budgets ──────────────────────────────────────────────────────

    async def upsert_budget(
        self,
        *,
        project_id: str,
        currency: str,
        ceiling: float,
        used: float,
        allow_paid_fallback: bool,
    ) -> ProjectBudget:
        record = await self._session.get(ProjectBudget, project_id)
        if record is None:
            record = ProjectBudget(project_id=project_id)
            self._session.add(record)
        record.currency = currency
        record.ceiling = ceiling
        record.used = used
        record.allow_paid_fallback = allow_paid_fallback
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return record

    async def get_budget(self, project_id: str) -> ProjectBudget | None:
        return await self._session.get(ProjectBudget, project_id)

    async def list_budgets(self) -> list[ProjectBudget]:
        rows = await self._session.scalars(select(ProjectBudget))
        return list(rows.all())

    # ── policies ─────────────────────────────────────────────────────

    _POLICY_DEFAULTS: dict[str, dict[str, Any]] = {
        "paid-fallback": {"enabled": False, "requires_budget": True, "project_id": None},
    }

    async def get_policy(self, policy_id: str) -> dict[str, Any]:
        record = await self._session.get(Policy, policy_id)
        if record is None:
            defaults = dict(self._POLICY_DEFAULTS.get(policy_id, {}))
            defaults["policy"] = policy_id
            return defaults
        return {
            "policy": record.policy_id,
            "enabled": record.enabled,
            "requires_budget": record.requires_budget,
            "project_id": record.project_id,
        }

    async def set_policy(
        self,
        policy_id: str,
        *,
        enabled: bool,
        requires_budget: bool,
        project_id: str | None,
    ) -> dict[str, Any]:
        record = await self._session.get(Policy, policy_id)
        if record is None:
            record = Policy(policy_id=policy_id)
            self._session.add(record)
        record.enabled = enabled
        record.requires_budget = requires_budget
        record.project_id = project_id
        await self._session.flush()
        return {
            "policy": policy_id,
            "enabled": enabled,
            "requires_budget": requires_budget,
            "project_id": project_id,
        }

    # ── alerts ───────────────────────────────────────────────────────

    async def create_alert(
        self, *, severity: str, message: str, source: str
    ) -> Alert:
        record = Alert(severity=severity, message=message, source=source)
        self._session.add(record)
        await self._session.flush()
        return record

    async def list_alerts(
        self, *, status: str | None = None, severity: str | None = None
    ) -> list[Alert]:
        stmt = select(Alert).order_by(Alert.created_at.desc())
        if status:
            stmt = stmt.where(Alert.status == status)
        if severity:
            stmt = stmt.where(Alert.severity == severity)
        rows = await self._session.scalars(stmt)
        return list(rows.all())

    async def get_alert(self, alert_id: str) -> Alert | None:
        return await self._session.get(Alert, alert_id)

    async def set_alert_status(self, record: Alert, status: str) -> Alert:
        record.status = status
        await self._session.flush()
        return record

    # ── settings ─────────────────────────────────────────────────────

    _SETTING_DEFAULTS: dict[str, str] = {"log_level": "INFO"}

    async def get_settings(self) -> dict[str, str]:
        rows = await self._session.scalars(select(ControlSetting))
        values = {row.key: row.value for row in rows.all()}
        merged = dict(self._SETTING_DEFAULTS)
        merged.update(values)
        return merged

    async def set_setting(self, key: str, value: str) -> None:
        record = await self._session.get(ControlSetting, key)
        if record is None:
            record = ControlSetting(key=key, value=value)
            self._session.add(record)
        else:
            record.value = value
        await self._session.flush()

    # ── audit ────────────────────────────────────────────────────────

    async def add_audit_event(self, action: str, metadata: dict[str, Any]) -> None:
        created_at = metadata.pop("created_at", None)
        if isinstance(created_at, str):
            try:
                parsed = datetime.fromisoformat(created_at)
                created_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                created_at = None
        self._session.add(
            AuditEvent(
                action=action,
                metadata_=dict(metadata),
                created_at=created_at or datetime.now(UTC),
            )
        )
        await self._session.flush()

    async def list_audit_events(
        self,
        *,
        action: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditEvent]:
        stmt = select(AuditEvent).order_by(AuditEvent.id.desc())
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        if since is not None:
            stmt = stmt.where(AuditEvent.created_at >= since)
        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)
        rows = await self._session.scalars(stmt)
        return list(rows.all())
