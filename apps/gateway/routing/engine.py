import time
from typing import List, Optional

from apps.gateway.routing.models import ResourceRef, ResourceCandidate
from apps.gateway.config.snapshot import RuntimeConfigSnapshot

class InMemoryCircuitRepository:
    def __init__(self):
        self._states = {}

    def _key(self, ref: ResourceRef) -> str:
        return f"{ref.provider_connection_id}:{ref.model_id}"

    def is_available(self, ref: ResourceRef) -> bool:
        key = self._key(ref)
        cooldown_until = self._states.get(key, 0.0)
        return cooldown_until <= time.monotonic()

    def trip(self, ref: ResourceRef, cooldown_seconds: float) -> None:
        key = self._key(ref)
        self._states[key] = time.monotonic() + cooldown_seconds

class RouterEngine:
    def __init__(self, snapshot: RuntimeConfigSnapshot, circuit_repository: Optional[InMemoryCircuitRepository] = None):
        self.snapshot = snapshot
        self.circuit_repository = circuit_repository or InMemoryCircuitRepository()

    def select_candidates(self, route_name: str) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []

        # Filter available primary candidates
        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]

        # Filter available fallback candidates
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]

        return primary + fallback

    def resolve_route(self, route_name: str) -> List[ResourceCandidate]:
        # Returns all candidates matching criteria (even fallbacks)
        return self.select_candidates(route_name)
