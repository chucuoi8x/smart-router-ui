"""Materialize Connection × enabled credentials × enabled models into real resources.

§20 provider criterion: every (connection, credential, model) triple becomes an
independent ``ResourceCandidate`` so circuit, quota, and health state cannot
cross-pollinate between credentials.

The compiler reads the live :class:`ProviderRegistryRepository` session and
emits a :class:`RuntimeConfigSnapshot` with one auto-generated route per
active connection.  The route's candidates are the cross product of enabled
credentials and enabled imported models.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.gateway.config.snapshot import (
    ConnectionConfig,
    RouteConfig,
    RuntimeConfigSnapshot,
)
from apps.gateway.db.models import ProviderConnection, ProviderCredential, ProviderModel
from apps.gateway.routing.models import ResourceCandidate, ResourceRef


class ProviderResourceCompiler:
    """Compile live registry rows into a :class:`RuntimeConfigSnapshot`."""

    async def compile(self, session: AsyncSession) -> RuntimeConfigSnapshot:
        connections = await session.execute(
            select(ProviderConnection).where(ProviderConnection.is_active.is_(True))
        )
        connections = list(connections.scalars().all())

        snapshot_connections: dict[str, ConnectionConfig] = {}
        snapshot_routes: dict[str, RouteConfig] = {}

        for connection in connections:
            snapshot_connections[connection.id] = ConnectionConfig(
                connection_id=connection.id,
                base_url=connection.base_url,
                auth_mode="bearer",
                token_env="",
            )

            credentials = await session.execute(
                select(ProviderCredential)
                .where(
                    ProviderCredential.connection_id == connection.id,
                    ProviderCredential.enabled.is_(True),
                )
                .order_by(
                    ProviderCredential.priority.desc(),
                    ProviderCredential.created_at,
                    ProviderCredential.id,
                )
            )
            credentials = list(credentials.scalars().all())

            models = await session.execute(
                select(ProviderModel)
                .where(
                    ProviderModel.connection_id == connection.id,
                    ProviderModel.enabled.is_(True),
                )
                .order_by(ProviderModel.model_id)
            )
            models = list(models.scalars().all())

            candidates: list[ResourceCandidate] = []
            for credential in credentials:
                for model in models:
                    ref = ResourceRef(
                        provider_connection_id=connection.id,
                        credential_scope=credential.id,
                        model_id=model.model_id,
                    )
                    candidates.append(
                        ResourceCandidate(
                            resource_ref=ref,
                            driver_id=connection.driver or connection.template_id,
                            weight=credential.weight,
                            metadata={
                                "credential_id": credential.id,
                                "credential_alias": credential.alias,
                                "model_id": model.model_id,
                                "enabled": True,
                            },
                        )
                    )

            snapshot_routes[connection.id] = RouteConfig(
                route_name=connection.id,
                strategy="priority",
                candidates=tuple(candidates),
                fallback=(),
                generated=True,
            )

        return RuntimeConfigSnapshot(
            connections=snapshot_connections,
            routes=snapshot_routes,
        )
