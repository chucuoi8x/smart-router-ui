"""Runtime-local immutable quota index.

The request path reads a snapshot created by a background refresh/update path.
It never invokes ``store.list_resources()`` or Redis ``SCAN`` while choosing a
candidate. Redis remains the authority for mutations/reservations.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from threading import Lock
from typing import Iterable

from .graph import QuotaGraph
from .reservations import QuotaAdmissionResult, QuotaReservationRequest, QuotaResource


def _deep_copy_resource(resource: QuotaResource) -> QuotaResource:
    """Return a QuotaResource with an isolated copy of window_metadata."""
    return replace(resource, window_metadata=copy.deepcopy(resource.window_metadata))


class RuntimeQuotaIndex:
    """Copy-on-write quota graph snapshot for request-time reads."""

    def __init__(self, *, max_depth: int = 5) -> None:
        self._max_depth = max_depth
        self._graph: QuotaGraph | None = None
        self._lock = Lock()

    @property
    def loaded(self) -> bool:
        return self._graph is not None

    def replace_all(self, resources: Iterable[QuotaResource]) -> None:
        """Atomically publish a fully rebuilt local index."""
        snapshot = [_deep_copy_resource(r) for r in resources]
        graph = QuotaGraph(_StaticQuotaStore(snapshot), max_depth=self._max_depth)
        graph.build_indexes(snapshot)
        with self._lock:
            self._graph = graph

    def apply_update(self, resource: QuotaResource) -> None:
        """Publish one resource update without touching Redis."""
        with self._lock:
            current = self._graph
            resources = current.get_all_resources() if current is not None else []
        by_id = {item.resource_id: item for item in resources}
        by_id[resource.resource_id] = resource
        self.replace_all(by_id.values())

    def snapshot(self, resource_id: str) -> QuotaResource:
        return _deep_copy_resource(self._require_graph().get_resource(resource_id))

    def get_resource(self, resource_id: str) -> QuotaResource:
        """Alias used by RouterEngine's graph-facing helper methods."""
        return _deep_copy_resource(self._require_graph().get_resource(resource_id))

    def check_many(self, requests: list[QuotaReservationRequest]) -> QuotaAdmissionResult:
        return self._require_graph().check_many(requests)

    def get_effective_remaining(self, resource_id: str) -> int:
        return self._require_graph().get_effective_remaining(resource_id)

    def resources_for(self, resource_ids: Iterable[str]) -> dict[str, QuotaResource]:
        graph = self._require_graph()
        result: dict[str, QuotaResource] = {}
        for resource_id in resource_ids:
            try:
                result[resource_id] = _deep_copy_resource(graph.get_resource(resource_id))
            except KeyError:
                continue
        return result

    def _require_graph(self) -> QuotaGraph:
        with self._lock:
            graph = self._graph
        if graph is None:
            raise RuntimeError("RuntimeQuotaIndex not loaded")
        return graph


class _StaticQuotaStore:
    """Read-only QuotaStore adapter used to build a graph without Redis I/O."""

    def __init__(self, resources: list[QuotaResource]) -> None:
        self._resources = list(resources)

    async def list_resources(self) -> list[QuotaResource]:
        return list(self._resources)
