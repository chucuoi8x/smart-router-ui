"""Runtime configuration authority that compiles DB-backed revisions into the live RouterEngine.

P0-20 §4.2/§4.3: The runtime must be controlled by the active PostgreSQL revision.
config.yaml is bootstrap-only.  Activation and rollback swap the runtime snapshot
atomically without restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from apps.gateway.config.compiler import LegacyConfigCompiler
from apps.gateway.config.snapshot import RuntimeConfigSnapshot
from apps.gateway.db.revisions import RevisionRepository
from apps.gateway.db.models import ConfigRevision
from sqlalchemy.ext.asyncio import AsyncSession

# P0 §20: durable revision activations are broadcast on this channel so every
# gateway instance converges to the active revision without a restart.
REVISION_ACTIVATION_CHANNEL = "smart-router:revision:activated"


class RuntimeConfigManager:
    """Owns the live runtime snapshot and swaps it atomically on revision activation."""

    def __init__(self, router: Any = None, redis_client: Any = None, session_factory: Any = None):
        self._router = router
        self._redis = redis_client
        self._session_factory = session_factory
        self._snapshot: Optional[RuntimeConfigSnapshot] = None
        self._active_revision_id: Optional[str] = None
        self._listener_task: Optional[asyncio.Task] = None

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
        """Load active revision from DB; fall back to bootstrap YAML migration.

        P0-20 §5.6/§20: the active revision is always compiled through
        ``compile_snapshot`` — no generated-route branch may bypass that
        validation (a bad revision must leave the last-known-good runtime
        untouched), and a restart must never rewrite historical snapshots.
        """
        repo = RevisionRepository(session)
        active = await repo.get_active()
        if active:
            snapshot = self.compile_snapshot(active.snapshot_data)
            await bind_runtime_credentials(self._router, session, snapshot)
            self._apply_snapshot(snapshot, active.id)
            return snapshot
        # No active DB revision → migrate config.yaml → DB → activate
        bootstrap_path = bootstrap_path or os.getenv("SMART_ROUTER_CONFIG", "config.yaml")
        snapshot = await self._bootstrap_from_yaml(bootstrap_path, session, repo)
        return snapshot

    async def materialize_provider_revision(self, session: AsyncSession) -> str:
        """Compile the live provider registry into a new immutable revision.

        P0-20 §5.6/§20: the :class:`ProviderResourceCompiler` is the only
        component that expands Connection × enabled credentials × enabled
        models into runtime candidates.  The materialized snapshot is stored
        as a fresh revision (history is never overwritten), validated through
        the same ``compile_snapshot`` gate as every other revision, and then
        activated so the data plane routes against real resources without a
        restart.
        """
        from apps.gateway.config.provider_resource_compiler import ProviderResourceCompiler

        materialized = await ProviderResourceCompiler().compile(session)
        draft = await RevisionRepository(session).create_draft(snapshot_to_dict(materialized))
        await self.activate(draft.id, session)
        return draft.id

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
        await bind_runtime_credentials(self._router, session, snapshot)
        self._apply_snapshot(snapshot, revision_id)
        await self.publish_activation(revision_id)
        return snapshot

    async def rollback(self, revision_id: str, session: AsyncSession) -> RuntimeConfigSnapshot:
        """Rollback to a prior revision atomically (alias for activate with audit context)."""
        return await self.activate(revision_id, session)

    # ── cross-instance propagation (P0 §20 distributed) ──────────────────
    async def publish_activation(self, revision_id: str) -> None:
        """Best-effort broadcast that ``revision_id`` is now the active revision.

        Called by the control plane *after* the durable commit and the local
        runtime swap.  Other gateway instances apply it in ``_apply_broadcast``.
        A publish failure never fails the activation itself — the next
        readiness convergence or restart re-reads the DB active pointer.
        """
        if self._redis is None:
            return
        try:
            await self._redis.publish(REVISION_ACTIVATION_CHANNEL, revision_id)
        except Exception:  # noqa: BLE001
            logging.getLogger("smart-router").warning(
                "revision activation publish failed for %s", revision_id, exc_info=True,
            )

    async def _apply_broadcast(self, revision_id: str) -> None:
        """Validate and swap runtime to a revision learned from another instance.

        Re-reads the durable revision (source of truth) rather than trusting the
        payload, and does NOT re-publish — preventing an infinite echo.
        """
        from apps.gateway.db.session import get_async_session_factory

        factory = self._session_factory or get_async_session_factory()
        async with factory() as session:
            try:
                repo = RevisionRepository(session)
                active = await repo.get_active()
                if active is None or active.id != revision_id:
                    return
                snapshot = await self.validate_revision(revision_id, session)
            except (KeyError, ValueError):
                return
        self._apply_snapshot(snapshot, revision_id)

    def start_listener(self) -> None:
        """Spawn the pub/sub subscriber task (no-op without Redis)."""
        if self._redis is None or self._listener_task is not None:
            return
        self._listener_task = asyncio.create_task(
            self._listen(), name="revision-propagation"
        )

    async def stop_listener(self) -> None:
        if self._listener_task is None:
            return
        self._listener_task.cancel()
        try:
            await self._listener_task
        except asyncio.CancelledError:
            pass
        self._listener_task = None

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(REVISION_ACTIVATION_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                if not data:
                    continue
                await self._apply_broadcast(str(data))
        finally:
            try:
                await pubsub.unsubscribe(REVISION_ACTIVATION_CHANNEL)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass


async def bind_runtime_credentials(
    router: Any,
    session: AsyncSession,
    snapshot: RuntimeConfigSnapshot,
) -> dict[str, str]:
    """Materialize the canonical credential secrets a snapshot references.

    P0-20 §5.3/§20: the durable truth for an upstream secret is the
    :class:`ProviderCredential` ciphertext row, selected by the candidate's
    ``credential_id`` — never the connection's legacy ``token_env``.  The
    runtime authority decrypts exactly the credentials referenced by the
    snapshot (test/dev fallback inside ``decrypt_secret`` keeps fixture
    ciphertexts usable) and hands the data plane a credential_id → secret
    map.  A router without the map slot (pure unit harness) is left alone.
    """
    from apps.gateway.db.provider_registry import ProviderRegistryRepository
    from apps.gateway.security.crypto import decrypt_secret

    registry = ProviderRegistryRepository(session)
    values: dict[str, str] = {}
    for route in snapshot.routes.values():
        for candidate in (*route.candidates, *route.fallback):
            credential_id = candidate.resource_ref.credential_scope
            if not credential_id:
                continue
            if credential_id in values:
                continue
            row = await registry.get_credential(str(credential_id))
            if row is None or not row.enabled:
                continue
            try:
                values[str(credential_id)] = decrypt_secret(row.credential_encrypted)
            except ValueError:
                logging.getLogger("smart-router").warning(
                    "credential %s could not be decrypted; candidate stays unresolved",
                    credential_id,
                )
    if router is not None:
        router._credential_values = values
    return values


def snapshot_to_legacy_config(snapshot: RuntimeConfigSnapshot) -> dict[str, Any]:
    """Project a snapshot into the SmartRouter legacy runtime dict shape.

    ``SmartRouter`` reads ``config['upstreams'][name]['auth']`` (nested) and
    ``config['routes']``; the compiler DB shape uses flat ``auth_mode``/
    ``token_env`` under ``connections``.  This is the single authority-side
    mapper so bootstrap and activation never hand SmartRouter a half-shaped
    config that would raise ``missing credential`` on the first data-plane
    request.  Must stay structurally identical to ``SmartRouter.update_snapshot``.
    """
    upstreams: dict[str, Any] = {}
    for conn_id, conn in snapshot.connections.items():
        upstreams[conn_id] = {
            "base_url": conn.base_url,
            "auth": {"mode": conn.auth_mode, "token_env": conn.token_env},
        }
    routes: dict[str, Any] = {}
    for route_name, route in snapshot.routes.items():
        def _cand(c: Any) -> dict[str, Any]:
            return {
                "upstream": c.resource_ref.provider_connection_id,
                "model": c.resource_ref.model_id,
                "weight": c.weight,
                **dict(c.metadata),
            }
        routes[route_name] = {
            "strategy": route.strategy,
            "candidates": [_cand(c) for c in route.candidates],
            "fallback": [_cand(c) for c in route.fallback],
            "generated": route.generated,
        }
    return {"upstreams": upstreams, "routes": routes}


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
        def _cand(c: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "upstream": c.resource_ref.provider_connection_id,
                "model": c.resource_ref.model_id,
                "credential_id": c.resource_ref.credential_scope,
                "weight": c.weight,
                # §4.6: the compiled resource already knows its driver — it must
                # survive the dict round-trip or reload falls back to a default.
                "driver_id": c.driver_id,
                "metadata": dict(c.metadata) if hasattr(c.metadata, "items") else dict(c.metadata),
            }
            return payload
        routes[route_name] = {
            "route_name": route.route_name,
            "strategy": route.strategy,
            "candidates": [_cand(c) for c in route.candidates],
            "fallback": [_cand(c) for c in route.fallback],
            "generated": route.generated,
        }
    return {"connections": connections, "routes": routes}