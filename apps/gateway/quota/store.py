"""QuotaStore abstraction with parent-chain and shared-group semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from threading import Lock
from typing import Any

from .reservations import (
    QuotaAdmissionResult,
    QuotaObservation,
    QuotaReservationRequest,
    QuotaResource,
    ReconciliationResult,
    ReservationBatchResult,
    ReservationResult,
)


class QuotaStore(ABC):
    """Abstract interface for quota storage backends."""

    @abstractmethod
    async def add_resource(self, resource: QuotaResource) -> QuotaResource:
        """Register or update a quota resource definition."""
        ...

    @abstractmethod
    async def snapshot(self, resource_id: str) -> QuotaResource:
        """Return the current state of a single resource."""
        ...

    @abstractmethod
    async def list_resources(self) -> list[QuotaResource]:
        """Return all resources currently stored."""
        ...

    @abstractmethod
    async def apply_observation(self, observation: QuotaObservation) -> QuotaResource:
        """Push a live metric observation into the store."""
        ...

    @abstractmethod
    async def check_many(self, requests: list[QuotaReservationRequest]) -> QuotaAdmissionResult:
        """Run a non-mutating admission check across multiple resources."""
        ...

    @abstractmethod
    async def reserve(
        self,
        *,
        resource_id: str,
        amount: int,
        reservation_id: str,
        risk_buffer: int = 0,
    ) -> ReservationResult:
        """Atomically reserve one resource."""
        ...

    @abstractmethod
    async def reserve_many(
        self,
        *,
        reservation_id: str,
        requests: list[QuotaReservationRequest],
    ) -> ReservationBatchResult:
        """Atomically reserve multiple resources with all-or-nothing semantics."""
        ...

    @abstractmethod
    async def reconcile(
        self,
        reservation_id: str,
        actual_by_resource: dict[str, int],
    ) -> ReconciliationResult:
        """Replace reserved usage with actual usage after completion."""
        ...

    @abstractmethod
    async def release(self, reservation_id: str) -> bool:
        """Release reserved capacity without marking usage as consumed."""
        ...


class InMemoryQuotaStore(QuotaStore):
    """Thread-safe in-memory QuotaStore.

    Effective remaining is the minimum of a resource's own remaining capacity,
    every peer in its shared group, and every ancestor in its parent chain. A
    reservation mutates the targeted resource/group and every ancestor so
    sequential sibling reservations cannot oversubscribe their shared parent.
    """

    def __init__(self, *, max_parent_depth: int = 10) -> None:
        self._resources: dict[str, QuotaResource] = {}
        self._reservations: dict[str, ReservationResult | ReservationBatchResult] = {}
        self._reservation_requests: dict[str, tuple[str, tuple[QuotaReservationRequest, ...]]] = {}
        self._reconciliations: dict[str, ReconciliationResult] = {}
        self._lock = Lock()
        self._max_parent_depth = max_parent_depth

    async def add_resource(self, resource: QuotaResource) -> QuotaResource:
        with self._lock:
            self._validate_parent_chain_for(resource)
            synced = self._sync_group_on_add(resource)
            self._resources[synced.resource_id] = synced
            return synced

    async def snapshot(self, resource_id: str) -> QuotaResource:
        with self._lock:
            return self._resource(resource_id)

    async def list_resources(self) -> list[QuotaResource]:
        with self._lock:
            return list(self._resources.values())

    async def apply_observation(self, observation: QuotaObservation) -> QuotaResource:
        with self._lock:
            current = self._resource(observation.resource_id)
            updated = replace(
                current,
                limit=observation.limit,
                used=observation.used,
                safety_buffer=current.safety_buffer if observation.safety_buffer is None else observation.safety_buffer,
                hard_limit=current.hard_limit if observation.hard_limit is None else observation.hard_limit,
                source=observation.source,
                confidence=observation.confidence,
            )
            self._resources[updated.resource_id] = updated
            if updated.shared_group_id is not None:
                for peer in self._group_peers(updated):
                    self._resources[peer.resource_id] = replace(
                        peer,
                        limit=updated.limit,
                        used=updated.used,
                        safety_buffer=updated.safety_buffer,
                        hard_limit=updated.hard_limit,
                    )
            return self._resource(observation.resource_id)

    async def check_many(self, requests: list[QuotaReservationRequest]) -> QuotaAdmissionResult:
        if not requests:
            raise ValueError("requests must not be empty")

        with self._lock:
            remaining = {request.resource_id: self._effective_remaining_single(request.resource_id) for request in requests}
            resource_requirements, group_requirements, parent_requirements = self._aggregate_requirements(requests)
            hard_failures: dict[str, int] = {}
            soft_pressure: dict[str, int] = {}

            for request in requests:
                resource = self._resource(request.resource_id)
                projected = resource_requirements[request.resource_id]
                if resource.shared_group_id:
                    projected = max(projected, group_requirements[resource.shared_group_id])
                for parent_id in self._parent_ids(request.resource_id):
                    projected = max(projected, parent_requirements[parent_id])

                shortfall = projected - remaining[request.resource_id]
                if shortfall > 0:
                    if resource.hard_limit:
                        hard_failures[request.resource_id] = shortfall
                    else:
                        soft_pressure[request.resource_id] = shortfall

            return QuotaAdmissionResult(
                accepted=not hard_failures,
                hard_failures=hard_failures,
                soft_pressure_by_resource=soft_pressure,
                remaining_by_resource=remaining,
            )

    async def reserve(
        self,
        *,
        resource_id: str,
        amount: int,
        reservation_id: str,
        risk_buffer: int = 0,
    ) -> ReservationResult:
        request = QuotaReservationRequest(resource_id, amount, risk_buffer)
        result = await self.reserve_many(reservation_id=reservation_id, requests=[request])
        return ReservationResult(
            reservation_id=reservation_id,
            resource_id=resource_id,
            amount=request.required,
            accepted=result.accepted,
            remaining=result.effective_remaining_by_resource.get(resource_id, result.remaining_by_resource.get(resource_id, 0)),
            reason=result.reason,
        )

    async def reserve_many(
        self,
        *,
        reservation_id: str,
        requests: list[QuotaReservationRequest],
    ) -> ReservationBatchResult:
        if not requests:
            raise ValueError("requests must not be empty")

        request_tuple = tuple(requests)
        with self._lock:
            existing = self._reservations.get(reservation_id)
            if existing is not None:
                self._ensure_same_reservation_request(reservation_id, request_tuple, kind="batch")
                if isinstance(existing, ReservationResult):
                    return ReservationBatchResult(
                        reservation_id=reservation_id,
                        requests=request_tuple,
                        accepted=existing.accepted,
                        remaining_by_resource={existing.resource_id: existing.remaining},
                        reason=existing.reason,
                        rejected_resource_id=None if existing.accepted else existing.resource_id,
                        effective_remaining_by_resource={existing.resource_id: existing.remaining},
                    )
                return existing
            if reservation_id in self._reservation_requests:
                raise ValueError("reservation_id conflict")

            for request in request_tuple:
                resource = self._resource(request.resource_id)
                if not resource.hard_limit:
                    raise ValueError(f"cannot reserve soft quota resource: {request.resource_id}")

            admission = self._check_many_locked(request_tuple)
            if not admission.accepted:
                rejected_resource_id = next(iter(admission.hard_failures))
                result = ReservationBatchResult(
                    reservation_id=reservation_id,
                    requests=request_tuple,
                    accepted=False,
                    remaining_by_resource={request.resource_id: self._resource(request.resource_id).remaining for request in request_tuple},
                    reason="quota_exceeded",
                    rejected_resource_id=rejected_resource_id,
                    effective_remaining_by_resource=admission.remaining_by_resource,
                )
                self._reservations[reservation_id] = result
                self._reservation_requests[reservation_id] = ("batch", request_tuple)
                return result

            resource_requirements, group_requirements, parent_requirements = self._aggregate_requirements(request_tuple)
            self._apply_resource_and_group_delta(resource_requirements, group_requirements)
            self._apply_parent_delta(parent_requirements)

            resource_ids = [request.resource_id for request in request_tuple]
            result = ReservationBatchResult(
                reservation_id=reservation_id,
                requests=request_tuple,
                accepted=True,
                remaining_by_resource={resource_id: self._resource(resource_id).remaining for resource_id in resource_ids},
                effective_remaining_by_resource=self._effective_remaining_many(resource_ids),
            )
            self._reservations[reservation_id] = result
            self._reservation_requests[reservation_id] = ("batch", request_tuple)
            return result

    async def reconcile(
        self,
        reservation_id: str,
        actual_by_resource: dict[str, int],
    ) -> ReconciliationResult:
        with self._lock:
            existing = self._reconciliations.get(reservation_id)
            if existing is not None:
                return existing
            if any(amount < 0 for amount in actual_by_resource.values()):
                raise ValueError("actual amounts must be non-negative")

            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                raise KeyError(f"unknown reservation_id: {reservation_id}")
            if not reservation.accepted:
                raise ValueError("cannot reconcile a rejected reservation")

            reserved_by_resource = self._reserved_by_resource(reservation)
            self._validate_actual_usage_keys(reserved_by_resource, actual_by_resource)
            actual = {resource_id: actual_by_resource[resource_id] for resource_id in reserved_by_resource}
            delta_requests = [
                QuotaReservationRequest(resource_id, actual[resource_id] - reserved)
                for resource_id, reserved in reserved_by_resource.items()
                if actual[resource_id] > reserved
            ]
            release_requests = [
                QuotaReservationRequest(resource_id, reserved - actual[resource_id])
                for resource_id, reserved in reserved_by_resource.items()
                if actual[resource_id] < reserved
            ]

            if release_requests:
                res_req, group_req, parent_req = self._aggregate_requirements(release_requests)
                self._apply_resource_and_group_delta(res_req, group_req, sign=-1)
                self._apply_parent_delta(parent_req, sign=-1)
            if delta_requests:
                res_req, group_req, parent_req = self._aggregate_requirements(delta_requests)
                self._apply_resource_and_group_delta(res_req, group_req)
                self._apply_parent_delta(parent_req)

            self._reservations.pop(reservation_id)
            released_by_resource = {
                resource_id: reserved - actual[resource_id]
                for resource_id, reserved in reserved_by_resource.items()
                if actual[resource_id] < reserved
            }
            overshoot_by_resource = {
                resource_id: actual[resource_id] - reserved
                for resource_id, reserved in reserved_by_resource.items()
                if actual[resource_id] > reserved
            }
            result = ReconciliationResult(
                reservation_id=reservation_id,
                reserved_by_resource=reserved_by_resource,
                actual_by_resource=actual,
                released_by_resource=released_by_resource,
                overshoot_by_resource=overshoot_by_resource,
                remaining_by_resource={resource_id: self._resource(resource_id).remaining for resource_id in reserved_by_resource},
            )
            self._reconciliations[reservation_id] = result
            return result

    async def release(self, reservation_id: str) -> bool:
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None or not reservation.accepted:
                return False
            requests = [
                QuotaReservationRequest(resource_id, amount)
                for resource_id, amount in self._reserved_by_resource(reservation).items()
            ]
            resource_requirements, group_requirements, parent_requirements = self._aggregate_requirements(requests)
            self._apply_resource_and_group_delta(resource_requirements, group_requirements, sign=-1)
            self._apply_parent_delta(parent_requirements, sign=-1)
            return True

    @classmethod
    async def from_repository(cls, repository: Any) -> "InMemoryQuotaStore":
        resources = await repository.list_resources()
        store = cls()
        for resource in resources:
            await store.add_resource(resource)
        return store

    def _check_many_locked(self, requests: tuple[QuotaReservationRequest, ...]) -> QuotaAdmissionResult:
        remaining = {request.resource_id: self._effective_remaining_single(request.resource_id) for request in requests}
        resource_requirements, group_requirements, parent_requirements = self._aggregate_requirements(requests)
        hard_failures: dict[str, int] = {}
        soft_pressure: dict[str, int] = {}
        for request in requests:
            resource = self._resource(request.resource_id)
            projected = resource_requirements[request.resource_id]
            if resource.shared_group_id:
                projected = max(projected, group_requirements[resource.shared_group_id])
            for parent_id in self._parent_ids(request.resource_id):
                projected = max(projected, parent_requirements[parent_id])
            shortfall = projected - remaining[request.resource_id]
            if shortfall > 0:
                if resource.hard_limit:
                    hard_failures[request.resource_id] = shortfall
                else:
                    soft_pressure[request.resource_id] = shortfall
        return QuotaAdmissionResult(
            accepted=not hard_failures,
            hard_failures=hard_failures,
            soft_pressure_by_resource=soft_pressure,
            remaining_by_resource=remaining,
        )

    def _aggregate_requirements(
        self,
        requests: tuple[QuotaReservationRequest, ...] | list[QuotaReservationRequest],
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        resource_requirements: dict[str, int] = {}
        group_requirements: dict[str, int] = {}
        parent_requirements: dict[str, int] = {}
        for request in requests:
            resource = self._resource(request.resource_id)
            resource_requirements[request.resource_id] = resource_requirements.get(request.resource_id, 0) + request.required
            if resource.shared_group_id:
                group_requirements[resource.shared_group_id] = group_requirements.get(resource.shared_group_id, 0) + request.required
            for parent_id in self._parent_ids(request.resource_id):
                parent_requirements[parent_id] = parent_requirements.get(parent_id, 0) + request.required
        return resource_requirements, group_requirements, parent_requirements

    def _apply_resource_and_group_delta(
        self,
        resource_requirements: dict[str, int],
        group_requirements: dict[str, int],
        *,
        sign: int = 1,
    ) -> None:
        mutated_groups: set[str] = set()
        for resource_id, amount in resource_requirements.items():
            resource = self._resource(resource_id)
            if resource.shared_group_id:
                group_id = resource.shared_group_id
                if group_id in mutated_groups:
                    continue
                peers = self._group_peers(resource)
                used = max(peer.used for peer in peers)
                self._set_group_used(group_id, max(0, used + sign * group_requirements[group_id]))
                mutated_groups.add(group_id)
            else:
                self._set_resource_used(resource_id, max(0, resource.used + sign * amount))

    def _apply_parent_delta(self, parent_requirements: dict[str, int], *, sign: int = 1) -> None:
        for parent_id, amount in parent_requirements.items():
            parent = self._resource(parent_id)
            self._set_resource_used(parent_id, max(0, parent.used + sign * amount))

    def _effective_remaining_many(self, resource_ids: list[str]) -> dict[str, int]:
        return {resource_id: self._effective_remaining_single(resource_id) for resource_id in resource_ids}

    def _effective_remaining_single(self, resource_id: str) -> int:
        resource = self._resource(resource_id)
        resource_remaining = self._remaining(resource)
        group_remaining = min((self._remaining(peer) for peer in self._group_peers(resource)), default=resource_remaining)
        parent_remaining = min(
            (self._remaining(self._resource(parent_id)) for parent_id in self._parent_ids(resource_id)),
            default=resource_remaining,
        )
        return min(resource_remaining, group_remaining, parent_remaining)

    def _parent_ids(self, resource_id: str) -> list[str]:
        chain: list[str] = []
        current_id = resource_id
        visited = {resource_id}
        for _ in range(self._max_parent_depth):
            parent_id = self._resource(current_id).parent_id
            if parent_id is None:
                return chain
            if parent_id in visited:
                raise ValueError(f"cycle detected in parent chain for {resource_id}")
            self._resource(parent_id)
            chain.append(parent_id)
            visited.add(parent_id)
            current_id = parent_id
        if self._resource(current_id).parent_id is not None:
            raise ValueError(f"parent chain exceeds max depth for {resource_id}")
        return chain

    def _validate_parent_chain_for(self, resource: QuotaResource) -> None:
        if resource.parent_id is None:
            return
        current_parent_id = resource.parent_id
        visited = {resource.resource_id}
        for _ in range(self._max_parent_depth):
            if current_parent_id in visited:
                raise ValueError(f"cycle detected involving {resource.resource_id}")
            visited.add(current_parent_id)
            parent = self._resources.get(current_parent_id)
            if parent is None or parent.parent_id is None:
                return
            current_parent_id = parent.parent_id
        raise ValueError(f"parent chain exceeds max depth for {resource.resource_id}")

    def _sync_group_on_add(self, resource: QuotaResource) -> QuotaResource:
        if resource.shared_group_id is None:
            return resource
        peers = [
            peer
            for peer in self._resources.values()
            if peer.shared_group_id == resource.shared_group_id and peer.resource_id != resource.resource_id
        ]
        if not peers:
            return resource
        synced_used = max(resource.used, *(peer.used for peer in peers))
        synced_limit = max(synced_used, min(resource.limit, *(peer.limit for peer in peers)))
        synced_safety_buffer = min(
            synced_limit,
            max(resource.safety_buffer, *(peer.safety_buffer for peer in peers)),
        )
        synced_hard_limit = resource.hard_limit or any(peer.hard_limit for peer in peers)
        for peer in peers:
            self._resources[peer.resource_id] = replace(
                peer,
                limit=synced_limit,
                used=synced_used,
                safety_buffer=synced_safety_buffer,
                hard_limit=synced_hard_limit,
            )
        return replace(
            resource,
            limit=synced_limit,
            used=synced_used,
            safety_buffer=synced_safety_buffer,
            hard_limit=synced_hard_limit,
        )

    def _group_peers(self, resource: QuotaResource) -> list[QuotaResource]:
        if resource.shared_group_id is None:
            return [resource]
        return [
            candidate
            for candidate in self._resources.values()
            if candidate.shared_group_id == resource.shared_group_id
        ]

    def _set_group_used(self, shared_group_id: str, used: int) -> None:
        for resource in list(self._resources.values()):
            if resource.shared_group_id == shared_group_id:
                self._resources[resource.resource_id] = replace(resource, used=used)

    def _set_resource_used(self, resource_id: str, used: int) -> None:
        resource = self._resource(resource_id)
        if resource.shared_group_id:
            self._set_group_used(resource.shared_group_id, used)
        else:
            self._resources[resource_id] = replace(resource, used=used)

    def _resource(self, resource_id: str) -> QuotaResource:
        try:
            return self._resources[resource_id]
        except KeyError as exc:
            raise KeyError(f"unknown quota resource_id: {resource_id}") from exc

    @staticmethod
    def _remaining(resource: QuotaResource) -> int:
        return max(0, resource.limit - resource.used - resource.safety_buffer)

    def _ensure_same_reservation_request(
        self,
        reservation_id: str,
        requests: tuple[QuotaReservationRequest, ...],
        *,
        kind: str,
    ) -> None:
        if self._reservation_requests.get(reservation_id) != (kind, requests):
            raise ValueError("reservation_id conflict")

    def _reserved_by_resource(self, reservation: ReservationResult | ReservationBatchResult) -> dict[str, int]:
        if isinstance(reservation, ReservationBatchResult):
            amounts: dict[str, int] = {}
            for request in reservation.requests:
                amounts[request.resource_id] = amounts.get(request.resource_id, 0) + request.required
            return amounts
        return {reservation.resource_id: reservation.amount}

    @staticmethod
    def _validate_actual_usage_keys(
        reserved_by_resource: dict[str, int],
        actual_by_resource: dict[str, int],
    ) -> None:
        reserved_ids = set(reserved_by_resource)
        actual_ids = set(actual_by_resource)
        missing = sorted(reserved_ids - actual_ids)
        if missing:
            raise ValueError(f"actual usage missing resource_id: {missing[0]}")
        unexpected = sorted(actual_ids - reserved_ids)
        if unexpected:
            raise ValueError(f"actual usage contains unknown resource_id: {unexpected[0]}")
