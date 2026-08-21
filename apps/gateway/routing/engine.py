from dataclasses import replace
import time
from typing import Any, List, Optional

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
    def __init__(
        self,
        snapshot: RuntimeConfigSnapshot,
        circuit_repository: Optional[InMemoryCircuitRepository] = None,
        quota_reservations: Optional[Any] = None,
    ):
        self.snapshot = snapshot
        self.circuit_repository = circuit_repository or InMemoryCircuitRepository()
        self.quota_reservations = quota_reservations

    def select_candidates(self, route_name: str) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []

        # Filter available primary candidates
        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]

        # Filter available fallback candidates
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]

        return self._quota_rank(primary) + self._quota_rank(fallback)

    def resolve_route(self, route_name: str) -> List[ResourceCandidate]:
        # Returns all candidates matching criteria (even fallbacks)
        return self.select_candidates(route_name)

    def _quota_rank(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        if self.quota_reservations is None:
            return candidates

        ranked: list[tuple[int, int, ResourceCandidate]] = []
        for index, candidate in enumerate(candidates):
            quota_candidate = self._check_candidate_quota(candidate)
            if quota_candidate is None:
                continue
            pressure = sum(
                quota_candidate.metadata.get("quota_soft_pressure_by_resource", {}).values()
            )
            ranked.append((pressure, index, quota_candidate))

        return [candidate for _, _, candidate in sorted(ranked, key=lambda item: (item[0], item[1]))]

    def _check_candidate_quota(self, candidate: ResourceCandidate) -> ResourceCandidate | None:
        from apps.gateway.quota.reservations import QuotaReservationRequest

        resource_id = self._quota_resource_id(candidate)
        try:
            admission = self.quota_reservations.check_many([
                QuotaReservationRequest(resource_id, amount=1)
            ])
        except KeyError:
            return candidate

        if not admission.accepted:
            return None

        metadata = {
            **candidate.metadata,
            "quota_resource_id": resource_id,
            "quota_remaining_by_resource": admission.remaining_by_resource,
        }
        if admission.soft_pressure_by_resource:
            metadata["quota_soft_pressure_by_resource"] = admission.soft_pressure_by_resource

        return replace(candidate, metadata=metadata)

    def _quota_resource_id(self, candidate: ResourceCandidate) -> str:
        return candidate.metadata.get(
            "quota_resource_id",
            f"model:{candidate.resource_ref.model_id}",
        )
