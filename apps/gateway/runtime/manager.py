"""Runtime configuration authority that compiles DB-backed revisions into the live RouterEngine.

P0-20 §4.2/§4.3: The runtime must be controlled by the active PostgreSQL revision.
config.yaml is bootstrap-only.  Activation and rollback swap the runtime snapshot
atomically without restart.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from apps.gateway.config.compiler import LegacyConfigCompiler
from apps.gateway.config.snapshot import RuntimeConfigSnapshot
from apps.gateway.db.revisions import RevisionRepository
from apps.gateway.db.models import ConfigRevision
from sqlalchemy.ext.asyncio import AsyncSession


class RuntimeConfigManager:
    """Owns the live runtime snapshot and swaps it atomically on revision activation."""

    def __init__(self, router: Any = None):
        self._router = router
        self._snapshot: Optional[RuntimeConfigSnapshot] = None
        self._active_revision_id: Optional[str] = None

    # ── properties ─────────────────────────────────────────────────────
    @property
    def snapshot(self) -> Optional[RuntimeConfigSnapshot]:
        return self._snapshot

    @property
    def active_revision_id(self) -> Optional[str]:
        return self._active_revision_id

    # ── lifecycle ──────────────────────────────────────────────────────
    async def load_initial(
        self,
        session: AsyncSession,
        bootstrap_path: Optional[str] = None,
    ) -> RuntimeConfigSnapshot:
        """Load active revision from DB; fall back to bootstrap YAML migration."""
        repo = RevisionRepository(session)
        active = await repo.get_active()
        if active:
            snapshot = self.compile_snapshot(active.snapshot_data)
            self._apply_snapshot(snapshot, active.id)
            return snapshot

        # No active DB revision → migrate config.yaml → DB → activate
        bootstrap_path = bootstrap_path or os.getenv("SMART_ROUTER_CONFIG", "config.yaml")
        snapshot = await self._bootstrap_from_yaml(bootstrap_path, session, repo)
        return snapshot

    async def _bootstrap_from_yaml(
        self,
        path: str,
        session: AsyncSession,
        repo: RevisionRepository,
    ) -> RuntimeConfigSnapshot:
        """Compile config.yaml into a draft, activate it, and load into runtime."""
        compiler = LegacyConfigCompiler()
        snapshot = compiler.compile_file(path)
        draft = await repo.create_draft(snapshot_to_dict(snapshot))
        await repo.activate(draft.id)
        await session.commit()
        self._apply_snapshot(snapshot, draft.id)
        return snapshot

    def compile_snapshot(self, snapshot_data: dict[str, Any]) -> RuntimeConfigSnapshot:
        """Compile and validate a snapshot before it may touch the runtime.

        §20 invalid-config-must-not-replace-LKG: every candidate/fallback must
        resolve to a declared connection.  Validation runs before any DB
        activation or runtime swap so a bad revision raises here and the
        last-known-good snapshot stays in place.
        """
        snapshot = LegacyConfigCompiler().compile_dict(snapshot_data)
        declared = set(snapshot.connections)
        for route in snapshot.routes.values():
            for candidate in (*route.candidates, *route.fallback):
                upstream = candidate.resource_ref.provider_connection_id
                if upstream not in declared:
                    raise ValueError(
                        f"route {route.route_name}: unknown connection {upstream!r}"
                    )
        return snapshot

    def _apply_snapshot(self, snapshot: RuntimeConfigSnapshot, revision_id: str) -> None:
        self._snapshot = snapshot
        self._active_revision_id = revision_id
        if self._router is not None and hasattr(self._router, "update_snapshot"):
            # SmartRouter.update_snapshot is synchronous and swaps the live
            # RouterEngine atomically; no task scheduling needed.
            self._router.update_snapshot(snapshot)

    async def validate_revision(self, revision_id: str, session: AsyncSession) -> RuntimeConfigSnapshot:
        """Fetch a revision and compile it without mutating DB or runtime.

        Raises ValueError for a snapshot that must never replace the
        last-known-good runtime (e.g. candidates pointing at unknown
        connections).  Control-plane endpoints call this before flipping the
        active pointer so durability and runtime stay consistent.
        """
        repo = RevisionRepository(session)
        revision = await repo.get_revision(revision_id)
        if revision is None:
            raise KeyError(f"Revision {revision_id} not found")
        return self.compile_snapshot(revision.snapshot_data)

    def set_active_runtime(self, snapshot: RuntimeConfigSnapshot, revision_id: str) -> None:
        """Swap the live runtime to an already-validated snapshot."""
        self._apply_snapshot(snapshot, revision_id)

    async def activate(self, revision_id: str, session: AsyncSession) -> RuntimeConfigSnapshot:
        """Atomically swap runtime snapshot to the given revision."""
        snapshot = await self.validate_revision(revision_id, session)
        repo = RevisionRepository(session)
        await repo.activate(revision_id)
        await session.commit()
        self._apply_snapshot(snapshot, revision_id)
        return snapshot

    async def rollback(self, revision_id: str, session: AsyncSession) -> RuntimeConfigSnapshot:
        """Rollback to a prior revision atomically (alias for activate with audit context)."""
        return await self.activate(revision_id, session)


def snapshot_to_dict(snapshot: RuntimeConfigSnapshot) -> dict[str, Any]:
    """Serialize a RuntimeConfigSnapshot into a plain dict for DB storage."""
    connections: dict[str, Any] = {}
    for conn_id, conn in snapshot.connections.items():
        connections[conn_id] = {
            "connection_id": conn.connection_id,
            "base_url": conn.base_url,
            "auth_mode": conn.auth_mode,
            "token_env": conn.token_env,
        }
    routes: dict[str, Any] = {}
    for route_name, route in snapshot.routes.items():
        routes[route_name] = {
            "route_name": route.route_name,
            "strategy": route.strategy,
            "candidates": [
                {
                    "upstream": c.resource_ref.provider_connection_id,
                    "model": c.resource_ref.model_id,
                    "credential_id": c.resource_ref.credential_scope,
                    "weight": c.weight,
                    "metadata": dict(c.metadata) if hasattr(c.metadata, "items") else dict(c.metadata),
                }
                for c in route.candidates
            ],
            "fallback": [
                {
                    "upstream": c.resource_ref.provider_connection_id,
                    "model": c.resource_ref.model_id,
                    "credential_id": c.resource_ref.credential_scope,
                    "weight": c.weight,
                    "metadata": dict(c.metadata) if hasattr(c.metadata, "items") else dict(c.metadata),
                }
                for c in route.fallback
            ],
            "generated": route.generated,
        }
    return {"connections": connections, "routes": routes}