"""Quota reservation domain services."""

from .reservations import (
    QuotaAdmissionResult,
    QuotaObservation,
    QuotaReservationRequest,
    QuotaResource,
    QuotaResourceRepository,
    InMemoryQuotaReservations,
    ReservationBatchResult,
    ReservationResult,
    ReconciliationResult,
)

try:
    from .redis_backend import RedisQuotaReservations  # noqa: F401

    __all__ = [
        "InMemoryQuotaReservations",
        "QuotaAdmissionResult",
        "QuotaObservation",
        "QuotaReservationRequest",
        "QuotaResource",
        "QuotaResourceRepository",
        "QuotaReservations",
        "RedisQuotaReservations",
        "ReservationBatchResult",
        "ReservationResult",
        "ReconciliationResult",
    ]
except ImportError:
    __all__ = [
        "InMemoryQuotaReservations",
        "QuotaAdmissionResult",
        "QuotaObservation",
        "QuotaReservationRequest",
        "QuotaResource",
        "QuotaResourceRepository",
        "ReservationBatchResult",
        "ReservationResult",
        "ReconciliationResult",
    ]
