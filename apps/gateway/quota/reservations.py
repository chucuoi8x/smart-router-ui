from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Lock


@dataclass(frozen=True)
class QuotaResource:
    resource_id: str
    scope: str
    metric: str
    limit: int
    window_seconds: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("limit must be non-negative")
        if self.used < 0:
            raise ValueError("used must be non-negative")
        if self.used > self.limit:
            raise ValueError("used cannot exceed limit")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

    @property
    def remaining(self) -> int:
        return self.limit - self.used


@dataclass(frozen=True)
class QuotaReservationRequest:
    resource_id: str
    amount: int

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be positive")


@dataclass(frozen=True)
class ReservationResult:
    reservation_id: str
    resource_id: str
    amount: int
    accepted: bool
    remaining: int
    reason: str | None = None


@dataclass(frozen=True)
class ReservationBatchResult:
    reservation_id: str
    requests: tuple[QuotaReservationRequest, ...]
    accepted: bool
    remaining_by_resource: dict[str, int]
    reason: str | None = None
    rejected_resource_id: str | None = None


class InMemoryQuotaReservations:
    def __init__(self) -> None:
        self._resources: dict[str, QuotaResource] = {}
        self._reservations: dict[str, ReservationResult | ReservationBatchResult] = {}
        self._lock = Lock()

    def add_resource(self, resource: QuotaResource) -> QuotaResource:
        with self._lock:
            self._resources[resource.resource_id] = resource
            return resource

    def snapshot(self, resource_id: str) -> QuotaResource:
        with self._lock:
            return self._resource(resource_id)

    def reserve(self, *, resource_id: str, amount: int, reservation_id: str) -> ReservationResult:
        if amount <= 0:
            raise ValueError("amount must be positive")

        with self._lock:
            resource = self._resource(resource_id)
            existing = self._reservations.get(reservation_id)
            if existing is not None:
                return existing

            if amount > resource.remaining:
                result = ReservationResult(
                    reservation_id=reservation_id,
                    resource_id=resource_id,
                    amount=amount,
                    accepted=False,
                    remaining=resource.remaining,
                    reason="quota_exceeded",
                )
                self._reservations[reservation_id] = result
                return result

            updated = replace(resource, used=resource.used + amount)
            self._resources[resource_id] = updated
            result = ReservationResult(
                reservation_id=reservation_id,
                resource_id=resource_id,
                amount=amount,
                accepted=True,
                remaining=updated.remaining,
            )
            self._reservations[reservation_id] = result
            return result

    def reserve_many(
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
                return existing

            projected_used: dict[str, int] = {}
            for request in request_tuple:
                resource = self._resource(request.resource_id)
                used = projected_used.get(request.resource_id, resource.used)
                if request.amount > resource.limit - used:
                    result = ReservationBatchResult(
                        reservation_id=reservation_id,
                        requests=request_tuple,
                        accepted=False,
                        remaining_by_resource=self._remaining_for(request_tuple),
                        reason="quota_exceeded",
                        rejected_resource_id=request.resource_id,
                    )
                    self._reservations[reservation_id] = result
                    return result
                projected_used[request.resource_id] = used + request.amount

            for resource_id, used in projected_used.items():
                self._resources[resource_id] = replace(self._resource(resource_id), used=used)

            result = ReservationBatchResult(
                reservation_id=reservation_id,
                requests=request_tuple,
                accepted=True,
                remaining_by_resource=self._remaining_for(request_tuple),
            )
            self._reservations[reservation_id] = result
            return result

    def release(self, reservation_id: str) -> bool:
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None or not reservation.accepted:
                return False

            if isinstance(reservation, ReservationBatchResult):
                for request in reservation.requests:
                    self._release_request(request)
                return True

            self._release_request(QuotaReservationRequest(reservation.resource_id, reservation.amount))
            return True

    def _release_request(self, request: QuotaReservationRequest) -> None:
        resource = self._resource(request.resource_id)
        self._resources[request.resource_id] = replace(
            resource,
            used=max(0, resource.used - request.amount),
        )

    def _remaining_for(self, requests: tuple[QuotaReservationRequest, ...]) -> dict[str, int]:
        return {
            request.resource_id: self._resource(request.resource_id).remaining
            for request in requests
        }

    def _resource(self, resource_id: str) -> QuotaResource:
        try:
            return self._resources[resource_id]
        except KeyError as exc:
            raise KeyError(f"unknown quota resource_id: {resource_id}") from exc
