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
    safety_buffer: int = 0

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("limit must be non-negative")
        if self.used < 0:
            raise ValueError("used must be non-negative")
        if self.used > self.limit:
            raise ValueError("used cannot exceed limit")
        if self.safety_buffer < 0:
            raise ValueError("safety_buffer must be non-negative")
        if self.safety_buffer > self.limit:
            raise ValueError("safety_buffer cannot exceed limit")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    @property
    def effective_remaining(self) -> int:
        return max(0, self.limit - self.used - self.safety_buffer)


@dataclass(frozen=True)
class QuotaReservationRequest:
    resource_id: str
    amount: int
    risk_buffer: int = 0

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        if self.risk_buffer < 0:
            raise ValueError("risk_buffer must be non-negative")

    @property
    def required(self) -> int:
        return self.amount + self.risk_buffer


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


@dataclass(frozen=True)
class ReconciliationResult:
    reservation_id: str
    reserved_by_resource: dict[str, int]
    actual_by_resource: dict[str, int]
    released_by_resource: dict[str, int]
    overshoot_by_resource: dict[str, int]
    remaining_by_resource: dict[str, int]


class InMemoryQuotaReservations:
    def __init__(self) -> None:
        self._resources: dict[str, QuotaResource] = {}
        self._reservations: dict[str, ReservationResult | ReservationBatchResult] = {}
        self._reconciliations: dict[str, ReconciliationResult] = {}
        self._lock = Lock()

    def add_resource(self, resource: QuotaResource) -> QuotaResource:
        with self._lock:
            self._resources[resource.resource_id] = resource
            return resource

    def snapshot(self, resource_id: str) -> QuotaResource:
        with self._lock:
            return self._resource(resource_id)

    def reserve(
        self,
        *,
        resource_id: str,
        amount: int,
        reservation_id: str,
        risk_buffer: int = 0,
    ) -> ReservationResult:
        request = QuotaReservationRequest(resource_id, amount, risk_buffer)

        with self._lock:
            resource = self._resource(resource_id)
            existing = self._reservations.get(reservation_id)
            if existing is not None:
                return existing

            if request.required > resource.effective_remaining:
                result = ReservationResult(
                    reservation_id=reservation_id,
                    resource_id=resource_id,
                    amount=request.required,
                    accepted=False,
                    remaining=resource.effective_remaining,
                    reason="quota_exceeded",
                )
                self._reservations[reservation_id] = result
                return result

            updated = replace(resource, used=resource.used + request.required)
            self._resources[resource_id] = updated
            result = ReservationResult(
                reservation_id=reservation_id,
                resource_id=resource_id,
                amount=request.required,
                accepted=True,
                remaining=updated.effective_remaining,
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
                if request.required > max(0, resource.limit - used - resource.safety_buffer):
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
                projected_used[request.resource_id] = used + request.required

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

    def reconcile(self, reservation_id: str, actual_by_resource: dict[str, int]) -> ReconciliationResult:
        if any(amount < 0 for amount in actual_by_resource.values()):
            raise ValueError("actual amounts must be non-negative")

        with self._lock:
            existing = self._reconciliations.get(reservation_id)
            if existing is not None:
                return existing

            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                raise KeyError(f"unknown reservation_id: {reservation_id}")
            if not reservation.accepted:
                raise ValueError("cannot reconcile a rejected reservation")

            reserved_by_resource = self._reserved_by_resource(reservation)
            actual = {
                resource_id: actual_by_resource.get(resource_id, 0)
                for resource_id in reserved_by_resource
            }
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

            for resource_id, actual_amount in actual.items():
                resource = self._resource(resource_id)
                reserved = reserved_by_resource[resource_id]
                self._resources[resource_id] = replace(
                    resource,
                    used=max(0, resource.used - reserved + actual_amount),
                )

            result = ReconciliationResult(
                reservation_id=reservation_id,
                reserved_by_resource=reserved_by_resource,
                actual_by_resource=actual,
                released_by_resource=released_by_resource,
                overshoot_by_resource=overshoot_by_resource,
                remaining_by_resource=self._remaining_by_resource(reserved_by_resource),
            )
            self._reconciliations[reservation_id] = result
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
            used=max(0, resource.used - request.required),
        )

    def _reserved_by_resource(self, reservation: ReservationResult | ReservationBatchResult) -> dict[str, int]:
        if isinstance(reservation, ReservationBatchResult):
            amounts: dict[str, int] = {}
            for request in reservation.requests:
                amounts[request.resource_id] = amounts.get(request.resource_id, 0) + request.required
            return amounts
        return {reservation.resource_id: reservation.amount}

    def _remaining_for(self, requests: tuple[QuotaReservationRequest, ...]) -> dict[str, int]:
        return {
            request.resource_id: self._resource(request.resource_id).remaining
            for request in requests
        }

    def _remaining_by_resource(self, amounts: dict[str, int]) -> dict[str, int]:
        return {
            resource_id: self._resource(resource_id).remaining
            for resource_id in amounts
        }

    def _resource(self, resource_id: str) -> QuotaResource:
        try:
            return self._resources[resource_id]
        except KeyError as exc:
            raise KeyError(f"unknown quota resource_id: {resource_id}") from exc
