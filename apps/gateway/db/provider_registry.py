"""Repository for provider connections, credentials, and imported models.

PR-05: the Control Plane provider registry is database-backed.  Records survive
process restarts, and nothing in this module keeps state in a process global.
The caller owns the :class:`AsyncSession` (and therefore the transaction);
methods ``flush`` so generated/derived values are readable immediately.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.gateway.db.models import ProviderConnection, ProviderCredential, ProviderModel


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProviderRegistryRepository:
    """Async repository for provider registry persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── connections ────────────────────────────────────────────────────
    async def create_connection(
        self,
        *,
        name: str,
        template_id: str,
        driver: str,
        base_url: str,
        credential_encrypted: str | None = None,
    ) -> ProviderConnection:
        conn = ProviderConnection(
            id=_new_id("conn"),
            name=name,
            template_id=template_id,
            driver=driver,
            base_url=base_url,
            credential_encrypted=credential_encrypted,
            is_active=True,
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        self._session.add(conn)
        await self._session.flush()
        return conn

    async def get_connection(self, connection_id: str) -> ProviderConnection | None:
        return await self._session.get(ProviderConnection, connection_id)

    async def require_connection(self, connection_id: str) -> ProviderConnection:
        """Return the connection or raise ``LookupError`` (handler maps to 404)."""
        conn = await self.get_connection(connection_id)
        if conn is None:
            raise LookupError(f"provider not found: {connection_id}")
        return conn

    async def list_connections(self) -> list[ProviderConnection]:
        result = await self._session.execute(
            select(ProviderConnection).order_by(ProviderConnection.created_at, ProviderConnection.id)
        )
        return list(result.scalars().all())

    async def update_connection(self, connection: ProviderConnection, **fields: Any) -> ProviderConnection:
        for key, value in fields.items():
            if hasattr(connection, key):
                setattr(connection, key, value)
        connection.updated_at = _utc_now()
        await self._session.flush()
        return connection

    async def set_connection_active(self, connection: ProviderConnection, active: bool) -> ProviderConnection:
        return await self.update_connection(connection, is_active=active)

    async def delete_connection(self, connection_id: str) -> bool:
        conn = await self._session.get(ProviderConnection, connection_id)
        if conn is None:
            return False
        await self._session.delete(conn)
        await self._session.flush()
        return True

    # ── credentials ────────────────────────────────────────────────────
    async def create_credential(
        self,
        *,
        connection_id: str,
        alias: str,
        credential_encrypted: str,
        enabled: bool = True,
        priority: int = 0,
        weight: int = 1,
        metadata_: dict[str, Any] | None = None,
    ) -> ProviderCredential:
        cred = ProviderCredential(
            id=_new_id("cred"),
            connection_id=connection_id,
            alias=alias,
            credential_encrypted=credential_encrypted,
            enabled=enabled,
            priority=priority,
            weight=weight,
            metadata_=metadata_ or {},
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        self._session.add(cred)
        await self._session.flush()
        return cred

    async def list_credentials(self, connection_id: str) -> list[ProviderCredential]:
        stmt = (
            select(ProviderCredential)
            .where(ProviderCredential.connection_id == connection_id)
            .order_by(ProviderCredential.priority.desc(), ProviderCredential.created_at, ProviderCredential.id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_credential(self, credential_id: str) -> ProviderCredential | None:
        return await self._session.get(ProviderCredential, credential_id)

    async def find_credential(self, connection_id: str, credential_id: str) -> ProviderCredential | None:
        stmt = select(ProviderCredential).where(
            ProviderCredential.id == credential_id,
            ProviderCredential.connection_id == connection_id,
        )
        return await self._session.scalar(stmt)

    async def has_credential(self, connection_id: str) -> bool:
        stmt = select(func.count()).select_from(ProviderCredential).where(
            ProviderCredential.connection_id == connection_id
        )
        return bool(await self._session.scalar(stmt))

    async def credential_counts_by_connection(self) -> dict[str, int]:
        """One grouped query: how many credential rows each connection owns."""
        stmt = select(ProviderCredential.connection_id, func.count()).group_by(
            ProviderCredential.connection_id
        )
        result = await self._session.execute(stmt)
        return {str(cid): int(total) for cid, total in result.all()}

    async def resolve_credential(self, connection: ProviderConnection) -> str | None:
        """Return the active credential ciphertext for a connection.

        Prefers the highest-priority enabled row in ``provider_credentials``;
        falls back to the legacy single-column ciphertext so connections created
        before PR-05 keep working.
        """
        stmt = (
            select(ProviderCredential)
            .where(
                ProviderCredential.connection_id == connection.id,
                ProviderCredential.enabled.is_(True),
            )
            .order_by(ProviderCredential.priority.desc(), ProviderCredential.created_at, ProviderCredential.id)
            .limit(1)
        )
        cred = await self._session.scalar(stmt)
        if cred is not None:
            return cred.credential_encrypted
        return connection.credential_encrypted

    async def delete_credential(self, credential_id: str) -> bool:
        cred = await self._session.get(ProviderCredential, credential_id)
        if cred is None:
            return False
        await self._session.delete(cred)
        await self._session.flush()
        return True

    # ── imported models ────────────────────────────────────────────────
    async def import_model(
        self,
        *,
        connection_id: str,
        model_id: str,
        metadata_: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> ProviderModel:
        existing = await self.find_model(connection_id=connection_id, model_id=model_id)
        if existing is not None:
            return existing
        model = ProviderModel(
            id=_new_id("pmod"),
            connection_id=connection_id,
            model_id=model_id,
            metadata_=metadata_ or {},
            enabled=enabled,
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        self._session.add(model)
        await self._session.flush()
        return model

    async def find_model(self, *, connection_id: str, model_id: str) -> ProviderModel | None:
        stmt = select(ProviderModel).where(
            ProviderModel.connection_id == connection_id,
            ProviderModel.model_id == model_id,
        )
        return await self._session.scalar(stmt)

    async def list_models(self, connection_id: str) -> list[ProviderModel]:
        stmt = (
            select(ProviderModel)
            .where(ProviderModel.connection_id == connection_id)
            .order_by(ProviderModel.model_id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
