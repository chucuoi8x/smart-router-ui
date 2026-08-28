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
from .store import QuotaStore, InMemoryQuotaStore
from .graph import QuotaGraph

try:
    from .redis_backend import RedisQuotaReservations, RedisQuotaStore  # noqa: F401

    __all__ = [
        "InMemoryQuotaReservations",
        "InMemoryQuotaStore",
        "QuotaAdmissionResult",
        "QuotaGraph",
        "QuotaObservation",
        "QuotaReservationRequest",
        "QuotaResource",
        "QuotaResourceRepository",
        "QuotaStore",
        "QuotaReservations",
        "RedisQuotaReservations",
        "RedisQuotaStore",
        "ReservationBatchResult",
        "ReservationResult",
        "ReconciliationResult",
    ]
except ImportError:
    __all__ = [
        "InMemoryQuotaReservations",
        "InMemoryQuotaStore",
        "QuotaAdmissionResult",
        "QuotaGraph",
        "QuotaObservation",
        "QuotaReservationRequest",
        "QuotaResource",
        "QuotaResourceRepository",
        "QuotaStore",
        "ReservationBatchResult",
        "ReservationResult",
        "ReconciliationResult",
    ]
