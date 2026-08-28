"""QuotaGraph domain logic for hierarchical quota resources."""

from __future__ import annotations

from threading import Lock

from .reservations import QuotaAdmissionResult, QuotaReservationRequest, QuotaResource
from .store import QuotaStore


class QuotaGraph:
    """Snapshot graph over quota resources.

    The graph builds parent-child and shared-group indexes from a QuotaStore, then
    computes effective remaining as the minimum of resource, shared group, and
    parent-chain remaining capacity. Call ``load`` again to refresh the snapshot.
    """

    def __init__(self, store: QuotaStore, *, max_depth: int = 5) -> None:
        self.store = store
        self._resources: dict[str, QuotaResource] = {}
        self._parents: dict[str, str | None] = {}
        self._children: dict[str, list[str]] = {}
        self._groups: dict[str, list[str]] = {}
        self._loaded = False
        self._lock = Lock()
        self._max_depth = max_depth

    async def load(self) -> None:
        """Load resources from the store and rebuild graph indexes."""
        resources = await self.store.list_resources()
        resources_by_id = {resource.resource_id: resource for resource in resources}
        parents = {resource.resource_id: resource.parent_id for resource in resources}
        children: dict[str, list[str]] = {}
        groups: dict[str, list[str]] = {}

        for resource in resources:
            if resource.parent_id:
                children.setdefault(resource.parent_id, []).append(resource.resource_id)
            if resource.shared_group_id:
                groups.setdefault(resource.shared_group_id, []).append(resource.resource_id)

        with self._lock:
            self._resources = resources_by_id
            self._parents = parents
            self._children = children
            self._groups = groups
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("QuotaGraph not loaded; call load() first")

    def get_resource(self, resource_id: str) -> QuotaResource:
        """Return a resource in the current snapshot."""
        self._ensure_loaded()
        try:
            return self._resources[resource_id]
        except KeyError as exc:
            raise KeyError(f"resource_id {resource_id} not in graph") from exc

    def get_parents(self, resource_id: str) -> list[str]:
        """Return the direct parent ID for a resource, if any."""
        self.get_resource(resource_id)
        parent = self._parents.get(resource_id)
        return [parent] if parent else []

    def get_children(self, resource_id: str) -> list[str]:
        """Return direct child resource IDs for a resource."""
        self.get_resource(resource_id)
        return list(self._children.get(resource_id, []))

    def get_group_peers(self, resource_id: str) -> list[str]:
        """Return resource IDs in the same shared group, including self."""
        resource = self.get_resource(resource_id)
        if resource.shared_group_id is None:
            return [resource_id]
        return list(self._groups.get(resource.shared_group_id, [resource_id]))

    def get_parent_chain(self, resource_id: str) -> list[str]:
        """Return ancestor resource IDs from parent toward root."""
        self.get_resource(resource_id)
        chain: list[str] = []
        current_id = resource_id
        visited = {resource_id}

        for _ in range(self._max_depth):
            parent_id = self._parents.get(current_id)
            if parent_id is None:
                return chain
            if parent_id in visited:
                raise ValueError(f"cycle detected in parent chain for {resource_id}")
            parent = self._resources.get(parent_id)
            if parent is None:
                raise KeyError(f"unknown parent quota resource_id: {parent_id}")
            chain.append(parent_id)
            visited.add(parent_id)
            current_id = parent_id

        if self._parents.get(current_id) is not None:
            raise ValueError(f"parent chain exceeds max depth for {resource_id}")
        return chain

    @staticmethod
    def _remaining(resource: QuotaResource) -> int:
        return max(0, resource.limit - resource.used - resource.safety_buffer)

    def get_effective_remaining(self, resource_id: str) -> int:
        """Compute min(resource_remaining, shared_group_remaining, parent_chain_remainings)."""
        resource = self.get_resource(resource_id)
        resource_remaining = self._remaining(resource)

        group_remaining = resource_remaining
        if resource.shared_group_id is not None:
            group_remaining = min(
                self._remaining(self._resources[peer_id])
                for peer_id in self.get_group_peers(resource_id)
            )

        chain = self.get_parent_chain(resource_id)
        parent_remaining = min(
            (self._remaining(self._resources[parent_id]) for parent_id in chain),
            default=resource_remaining,
        )
        return min(resource_remaining, group_remaining, parent_remaining)

    def get_effective_remaining_many(self, resource_ids: list[str]) -> dict[str, int]:
        """Compute effective remaining for many resource IDs."""
        return {resource_id: self.get_effective_remaining(resource_id) for resource_id in resource_ids}

    def check_many(self, requests: list[QuotaReservationRequest]) -> QuotaAdmissionResult:
        """Run a snapshot admission check through graph effective remaining."""
        if not requests:
            raise ValueError("requests must not be empty")

        hard_failures: dict[str, int] = {}
        soft_pressure: dict[str, int] = {}
        remaining_by_resource: dict[str, int] = {}
        projected_by_resource: dict[str, int] = {}
        projected_by_group: dict[str, int] = {}
        projected_by_parent: dict[str, int] = {}

        for request in requests:
            resource = self.get_resource(request.resource_id)
            effective = self.get_effective_remaining(request.resource_id)
            remaining_by_resource[request.resource_id] = effective

            projected = projected_by_resource.get(request.resource_id, 0) + request.required
            if resource.shared_group_id:
                projected_by_group[resource.shared_group_id] = (
                    projected_by_group.get(resource.shared_group_id, 0) + request.required
                )
                projected = max(projected, projected_by_group[resource.shared_group_id])
            for parent_id in self.get_parent_chain(request.resource_id):
                projected_by_parent[parent_id] = projected_by_parent.get(parent_id, 0) + request.required
                projected = max(projected, projected_by_parent[parent_id])

            shortfall = projected - effective
            if shortfall > 0:
                if resource.hard_limit:
                    hard_failures[request.resource_id] = shortfall
                else:
                    soft_pressure[request.resource_id] = shortfall
            projected_by_resource[request.resource_id] = projected_by_resource.get(request.resource_id, 0) + request.required

        return QuotaAdmissionResult(
            accepted=not hard_failures,
            hard_failures=hard_failures,
            soft_pressure_by_resource=soft_pressure,
            remaining_by_resource=remaining_by_resource,
        )

    def get_all_resources(self) -> list[QuotaResource]:
        """Return all resources in the current snapshot."""
        self._ensure_loaded()
        return list(self._resources.values())

    def get_all_resource_ids(self) -> list[str]:
        """Return all resource IDs in the current snapshot."""
        self._ensure_loaded()
        return list(self._resources.keys())

    @property
    def loaded(self) -> bool:
        return self._loaded
