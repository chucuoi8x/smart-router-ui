from dataclasses import replace
import asyncio
import threading
import time
from typing import Any, List, Optional

from apps.gateway.routing.models import ResourceRef, ResourceCandidate
from apps.gateway.config.snapshot import RuntimeConfigSnapshot
from apps.gateway.routing.scoring import ScoringConfig

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
        scoring_config: Optional[ScoringConfig] = None,
    ):
        self.snapshot = snapshot
        self.circuit_repository = circuit_repository or InMemoryCircuitRepository()
        # Auto-wrap sync backends so async methods can await them.
        if quota_reservations is not None and not asyncio.iscoroutinefunction(
            getattr(quota_reservations, "check_many", None),
        ):
            from apps.gateway.quota.adapter import AsyncQuotaFacade
            quota_reservations = AsyncQuotaFacade(quota_reservations)  # type: ignore[assignment]
        self.quota_reservations = quota_reservations
        self._scoring_config = scoring_config
        self._score_calculator: Any | None = None
        if scoring_config and scoring_config.enabled:
            from apps.gateway.routing.scoring import SmartScoreCalculator
            self._score_calculator = SmartScoreCalculator(config=scoring_config)

    def _quota_snap(self, resource_id: str) -> Any:
        """Sync-friendly snapshot that works with both sync and async backends.

        When inside a running event loop (pytest-asyncio), spawns a helper
        thread with its own event loop so we can ``await`` coroutine snapshots
        without triggering *RuntimeError: This event loop is already running*.
        """
        snap = self.quota_reservations.snapshot  # type: ignore[union-attr]
        if asyncio.iscoroutinefunction(snap):
            try:
                _loop = asyncio.get_running_loop()
            except RuntimeError:
                _loop = None
            if _loop is not None:
                # Nested-loop-safe executor: run an inner fresh loop in a thread
                _result: list[Any] = []
                _error: list[BaseException] = []

                def _runner():
                    _inner = asyncio.new_event_loop()
                    try:
                        coro = snap(resource_id)
                        _result.append(_inner.run_until_complete(coro))
                    except BaseException as exc:
                        _error.append(exc)
                    finally:
                        _inner.close()

                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join(timeout=10)
                if _error:
                    raise _error[0]
                return _result[0] if _result else None
            # No running loop — just create one for this call
            _fresh = asyncio.new_event_loop()
            try:
                return _fresh.run_until_complete(snap(resource_id))
            finally:
                _fresh.close()
        return snap(resource_id)

    def select_candidates(self, route_name: str) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []

        # Filter available primary candidates
        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]

        # Filter available fallback candidates
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]

        scored_primary = self._quota_rank_sync(primary)
        scored_fallback = self._quota_rank_sync(fallback)
        combined = list(self._apply_smart_scoring(scored_primary)) + list(self._apply_smart_scoring(scored_fallback))
        return combined

    def resolve_route(self, route_name: str) -> List[ResourceCandidate]:
        """Return candidates matching criteria (even fallbacks).

        When running under an event loop (Redis-backed quotas), creates a new
        loop so sync callers still work. Falls back to pure-sync path otherwise.
        """
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop — run sync subset only (no Redis quotas)
            route = self.snapshot.routes.get(route_name)
            if route is None:
                return []
            primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]
            fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]
            return list(self._apply_smart_scoring(primary)) + list(self._apply_smart_scoring(fallback))

        # Running loop detected — create a fresh one for this task
        fresh_loop = asyncio.new_event_loop()
        try:
            return fresh_loop.run_until_complete(self.select_candidates(route_name))  # type: ignore[return-value]
        finally:
            fresh_loop.close()

    def _quota_rank_sync(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        """Sync entry-point that delegates to async ``_quota_rank`` via a short-lived loop."""
        if self.quota_reservations is None:
            return candidates
        import asyncio
        fresh_loop = asyncio.new_event_loop()
        try:
            ranked = fresh_loop.run_until_complete(self._quota_rank(candidates))
            return list(ranked)
        finally:
            fresh_loop.close()

    async def _quota_rank(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        if self.quota_reservations is None:
            return candidates

        ranked: list[tuple[int, int, ResourceCandidate]] = []
        for index, candidate in enumerate(candidates):
            quota_candidate = await self._check_candidate_quota(candidate)
            if quota_candidate is None:
                continue
            pressure = sum(
                quota_candidate.metadata.get("quota_soft_pressure_by_resource", {}).values()
            )
            ranked.append((pressure, index, quota_candidate))

        return [candidate for _, _, candidate in sorted(ranked, key=lambda item: (item[0], item[1]))]

    async def _check_candidate_quota(self, candidate: ResourceCandidate) -> ResourceCandidate | None:
        from apps.gateway.quota.reservations import QuotaReservationRequest

        resource_ids = self._known_quota_resource_ids(self._quota_resource_ids(candidate))
        if not resource_ids:
            return candidate

        admission = await self.quota_reservations.check_many([
            QuotaReservationRequest(resource_id, amount=1)
            for resource_id in resource_ids
        ])

        if not admission.accepted:
            return None

        metadata = {
            **candidate.metadata,
            "quota_remaining_by_resource": admission.remaining_by_resource,
        }
        if self._has_valid_quota_resource_ids(candidate):
            metadata["quota_resource_ids"] = resource_ids
        else:
            metadata["quota_resource_id"] = resource_ids[0]
        if admission.soft_pressure_by_resource:
            metadata["quota_soft_pressure_by_resource"] = admission.soft_pressure_by_resource

        return replace(candidate, metadata=metadata)

    def _known_quota_resource_ids(self, resource_ids: list[str]) -> list[str]:
        known: list[str] = []
        seen_groups: set[str] = set()
        for resource_id in resource_ids:
            try:
                resource = self._quota_snap(resource_id)
            except (KeyError, TypeError, ValueError):
                continue
            group_key = resource.shared_group_id or resource.resource_id
            if group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            known.append(resource_id)
        return known

    def _quota_resource_ids(self, candidate: ResourceCandidate) -> list[str]:
        if self._has_valid_quota_resource_ids(candidate):
            return list(candidate.metadata["quota_resource_ids"])
        return [self._quota_resource_id(candidate)]

    def _has_valid_quota_resource_ids(self, candidate: ResourceCandidate) -> bool:
        resource_ids = candidate.metadata.get("quota_resource_ids")
        return (
            isinstance(resource_ids, (list, tuple))
            and len(resource_ids) > 0
            and all(isinstance(resource_id, str) and resource_id for resource_id in resource_ids)
        )

    def _quota_resource_id(self, candidate: ResourceCandidate) -> str:
        return candidate.metadata.get(
            "quota_resource_id",
            f"model:{candidate.resource_ref.model_id}",
        )

    def _apply_smart_scoring(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        """Re-order candidates via smart scoring if enabled and calculator exists."""
        if self._score_calculator is None or not candidates:
            return candidates
        try:
            keys = [c.resource_ref.provider_connection_id + ":" + c.resource_ref.model_id for c in candidates]
            # Build minimal metrics dict from catalog/quota data
            from apps.gateway.routing.scoring import CandidateMetrics
            metrics: dict[str, CandidateMetrics] = {}
            prices = getattr(self.snapshot, "_prices", {}) or {}
            for key in keys:
                model_part = key.split(":", 1)[-1]
                price_info = prices.get(model_part, {})
                eff_remaining = 0
                lim = 0
                burn_urgency = 0.0
                # Try quota snapshot for each candidate
                rid = f"model:{model_part}"
                if self.quota_reservations:
                    try:
                        res = self._quota_snap(rid)
                        eff_remaining = getattr(res, "effective_remaining", 0)
                        lim = getattr(res, "limit", 0)
                    except (KeyError, TypeError, ValueError):
                        pass
                if lim > 0 and eff_remaining >= 0:
                    burn_urgency = 1.0 - (eff_remaining / lim)
                metrics[key] = CandidateMetrics(
                    price_per_million_output=price_info.get("output_per_million"),
                    effective_remaining=eff_remaining,
                    limit=lim,
                    burn_rate_urgency=burn_urgency,
                )
            scored = self._score_calculator.compute_scores(
                candidates=candidates,
                candidate_keys=keys,
                metrics_by_key=metrics,
            )
            return [c for c, _ in scored]
        except Exception as exc:
            return candidates  # Graceful degradation
