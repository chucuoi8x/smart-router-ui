"""SQLAlchemy async engine and session lifecycle for the gateway.

Engine and session factory are lazily initialised on first use so that
importing this module never touches disk or spawns connections.  A pair
of functions -- ``init_engine`` / ``dispose_engine`` -- are intended to
be called from the FastAPI lifespan (startup / shutdown).
"""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# ── globals (all None until init_engine is called) ────────────────────
_engine: Optional[AsyncEngine] = None
_async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _get_database_url() -> str:
    """Return DATABASE_URL from environment, falling back to a local dev default."""
    return os.getenv(
        'DATABASE_URL',
        'postgresql+asyncpg://user:pass@localhost/smart_router',
    )


# ── public initialisers ───────────────────────────────────────────────

def init_engine(url: str | None = None) -> AsyncEngine:
    """Create (or return) the global SQLAlchemy async engine.

    Idempotent – calling multiple times returns the existing engine
    unless *url* differs, in which case it disposes the old one and
    creates a fresh instance.
    """
    global _engine, _async_session_factory
    target_url = url or _get_database_url()

    if _engine is not None:
        # Verify URL matches (ignore connection-string params ordering)
        if _engine.url.__str__() == target_url:
            return _engine
        # Different URL requested → dispose old and recreate
        dispose_engine()

    _engine = create_async_engine(target_url, echo=False)
    _async_session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    return _engine


def dispose_engine() -> None:
    """Close pools and invalidate the global engine/session factory."""
    global _engine, _async_session_factory
    if _engine is not None:
        _engine.dispose()
        _engine = None
    _async_session_factory = None


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, initialising the engine if needed.

    This function exists so code that needs a factory immediately can
    still work.  For new FastAPI routes prefer the DI dependency in
    ``apps.gateway.db.dependencies.get_session``.
    """
    if _engine is None:
        init_engine()
    assert _async_session_factory is not None  # type - ignore
    return _async_session_factory
