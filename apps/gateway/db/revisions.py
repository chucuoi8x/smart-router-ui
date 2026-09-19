"""DB-backed Config Revision persistence (P0-05)."""
from __future__ import annotations

from datetime import UTC, datetime
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.gateway.db.models import ConfigRevision


class RevisionRepository:
    """Async repository for ConfigRevision persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_draft(self, snapshot_data: dict[str, Any]) -> ConfigRevision:
        revision = ConfigRevision(
            id=f"rev_{uuid.uuid4().hex[:12]}",
            snapshot_data=snapshot_data,
            is_active=False,
            created_at=datetime.now(UTC),
        )
        self._session.add(revision)
        await self._session.flush()
        return revision

    async def get_revision(self, revision_id: str) -> ConfigRevision | None:
        return await self._session.get(ConfigRevision, revision_id)

    async def get_active(self) -> ConfigRevision | None:
        result = await self._session.execute(
            select(ConfigRevision).where(ConfigRevision.is_active == True)
        )
        return result.scalar_one_or_none()

    async def activate(self, revision_id: str) -> ConfigRevision:
        # Atomic transition
        await self._session.execute(
            update(ConfigRevision)
            .where(ConfigRevision.is_active == True)
            .values(is_active=False)
        )
        target = await self.get_revision(revision_id)
        if not target:
            raise KeyError(f"Revision {revision_id} not found")
        
        target.is_active = True
        target.activated_at = datetime.now(UTC)
        await self._session.flush()
        return target

    async def list_revisions(self) -> list[ConfigRevision]:
        result = await self._session.execute(
            select(ConfigRevision).order_by(ConfigRevision.created_at.desc())
        )
        return list(result.scalars().all())
