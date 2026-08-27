"""FastAPI dependency injection helpers for the database layer.

Exports two dependencies that can be used in route handlers via
``Depends``:

- ``get_session`` — yields a scoped :class:`~sqlalchemy.ext.asyncio.AsyncSession`
- ``get_usage_ledger_repo`` — yields a
  :class:`~apps.gateway.usage.ledger.UsageLedgerRepository` backed by that session
"""
from __future__ import annotations

from typing import AsyncGenerator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.gateway.db.session import get_async_session_factory
from apps.gateway.usage.ledger import UsageLedgerRepository


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a single async session per request and close it afterwards.

    The session factory is lazily initialised from the module-level
    ``DATABASE_URL`` environment variable so that tests can still run
    without a real database (they should inject their own mock session
    instead of using this dependency).
    """
    factory = get_async_session_factory()
    async with factory() as session:
        yield session


async def get_usage_ledger_repo(
    session: AsyncSession = Depends(get_session),
) -> UsageLedgerRepository:
    """Yield a ``UsageLedgerRepository`` backed by the request session."""
    repo = UsageLedgerRepository(session)
    try:
        yield repo
    finally:
        # Ensure any pending work is flushed before the session closes
        await session.commit()
