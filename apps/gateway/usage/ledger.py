from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_PRIVATE_METADATA_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "provider_secret",
    "raw_prompt",
    "raw_response",
    "request_body",
    "response_body",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _public_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if key.lower() not in _PRIVATE_METADATA_KEYS
    }


@dataclass(frozen=True)
class UsageEvent:
    request_id: str
    attempt_id: str
    provider_connection_id: str
    credential_id: str | None
    model_resource_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    native_metric: str | None = None
    native_amount: float | None = None
    actual_cost: float | None = None
    currency: str | None = None
    source: str = "generic_estimate"
    confidence: str = "estimated"
    estimated: bool = True
    observed_at: datetime = field(default_factory=_utc_now)

    @classmethod
    def from_parsed_usage(
        cls,
        *,
        request_id: str,
        attempt_id: str,
        provider_connection_id: str,
        credential_id: str | None,
        model_resource_id: str,
        usage: dict[str, Any],
    ) -> "UsageEvent":
        input_tokens = _as_int(usage.get("input_tokens"))
        output_tokens = _as_int(usage.get("output_tokens"))
        total_tokens = _as_int(usage.get("total_tokens")) or input_tokens + output_tokens
        source = usage.get("source", "generic_estimate")
        confidence = usage.get("confidence", "estimated")
        estimated = bool(usage.get("estimated", source != "provider_api" or confidence != "exact"))
        return cls(
            request_id=request_id,
            attempt_id=attempt_id,
            provider_connection_id=provider_connection_id,
            credential_id=credential_id,
            model_resource_id=model_resource_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=_as_int(usage.get("cached_input_tokens")),
            cache_write_tokens=_as_int(usage.get("cache_write_tokens")),
            reasoning_tokens=_as_int(usage.get("reasoning_tokens")),
            total_tokens=total_tokens,
            native_metric=usage.get("native_metric"),
            native_amount=usage.get("native_amount"),
            actual_cost=usage.get("actual_cost"),
            currency=usage.get("currency"),
            source=source,
            confidence=confidence,
            estimated=estimated,
        )

    def to_db_model(self, *, id: str | None = None):
        from apps.gateway.db.models import UsageLedger

        return UsageLedger(
            id=id,
            request_id=self.request_id,
            attempt_id=self.attempt_id,
            provider_id=self.provider_connection_id,
            credential_id=self.credential_id,
            model=self.model_resource_id,
            prompt_tokens=self.input_tokens,
            completion_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens,
            total_tokens=self.total_tokens,
            native_metric=self.native_metric,
            native_amount=self.native_amount,
            estimated_cost=self.actual_cost or 0.0,
            currency=self.currency,
            source=self.source,
            confidence=self.confidence,
            estimated=self.estimated,
            created_at=self.observed_at,
        )


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    route_id: str
    logical_model: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "route_id": self.route_id,
            "logical_model": self.logical_model,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }

    def to_db_model(self):
        from apps.gateway.db.models import RequestLedger

        return RequestLedger(
            request_id=self.request_id,
            route_id=self.route_id,
            logical_model=self.logical_model,
            metadata_=self.metadata,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class AttemptRecord:
    request_id: str
    attempt_id: str
    provider_connection_id: str
    model_resource_id: str
    status: str
    created_at: datetime = field(default_factory=_utc_now)

    def to_db_model(self):
        from apps.gateway.db.models import AttemptLedger

        return AttemptLedger(
            request_id=self.request_id,
            attempt_id=self.attempt_id,
            provider_id=self.provider_connection_id,
            model=self.model_resource_id,
            status=self.status,
            created_at=self.created_at,
        )


class UsageLedgerRepository:
    def __init__(self, session) -> None:
        self._session = session

    async def record_request(self, record: RequestRecord, *, commit: bool = False):
        row = record.to_db_model()
        self._session.add(row)
        await self._session.flush()
        if commit:
            await self._session.commit()
        return row

    async def record_attempt(self, record: AttemptRecord, *, commit: bool = False):
        row = record.to_db_model()
        self._session.add(row)
        await self._session.flush()
        if commit:
            await self._session.commit()
        return row

    async def record_usage(self, event: UsageEvent, *, id: str | None = None, commit: bool = False):
        row = event.to_db_model(id=id)
        self._session.add(row)
        await self._session.flush()
        if commit:
            await self._session.commit()
        return row


class InMemoryUsageLedger:
    def __init__(self) -> None:
        self._requests: dict[str, RequestRecord] = {}
        self._attempts: dict[str, AttemptRecord] = {}
        self._events: list[UsageEvent] = []

    def record_request(
        self,
        *,
        request_id: str,
        route_id: str,
        logical_model: str,
        metadata: dict[str, Any] | None = None,
    ) -> RequestRecord:
        record = RequestRecord(
            request_id=request_id,
            route_id=route_id,
            logical_model=logical_model,
            metadata=_public_metadata(metadata),
        )
        self._requests[request_id] = record
        return record

    def record_attempt(
        self,
        *,
        request_id: str,
        attempt_id: str,
        provider_connection_id: str,
        model_resource_id: str,
        status: str,
    ) -> AttemptRecord:
        if request_id not in self._requests:
            raise KeyError(f"unknown request_id: {request_id}")
        record = AttemptRecord(
            request_id=request_id,
            attempt_id=attempt_id,
            provider_connection_id=provider_connection_id,
            model_resource_id=model_resource_id,
            status=status,
        )
        self._attempts[attempt_id] = record
        return record

    def record_usage(self, event: UsageEvent) -> UsageEvent:
        if event.request_id not in self._requests:
            raise KeyError(f"unknown request_id: {event.request_id}")
        if event.attempt_id not in self._attempts:
            raise KeyError(f"unknown attempt_id: {event.attempt_id}")
        self._events.append(event)
        return event

    def request_totals(self, request_id: str) -> dict[str, int]:
        events = [event for event in self._events if event.request_id == request_id]
        attempts = [attempt for attempt in self._attempts.values() if attempt.request_id == request_id]
        return {
            "input_tokens": sum(event.input_tokens for event in events),
            "output_tokens": sum(event.output_tokens for event in events),
            "cached_input_tokens": sum(event.cached_input_tokens for event in events),
            "cache_write_tokens": sum(event.cache_write_tokens for event in events),
            "reasoning_tokens": sum(event.reasoning_tokens for event in events),
            "total_tokens": sum(event.total_tokens for event in events),
            "attempt_count": len(attempts),
            "usage_event_count": len(events),
        }
