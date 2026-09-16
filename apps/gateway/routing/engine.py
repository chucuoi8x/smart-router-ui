from dataclasses import replace
import asyncio
import threading
import time
from typing import Any, List, Optional

from apps.gateway.routing.models import ResourceRef, ResourceCandidate
from apps.gateway.config.snapshot import RuntimeConfigSnapshot
from apps.gateway.routing.scoring import ScoringConfig
from apps.gateway.routing.presets import filter_candidates_for_policy

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

    def _run_coro_sync(self, coro: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        result: list[Any] = []
        error: list[BaseException] = []

        def runner():
            loop = asyncio.new_event_loop()
            try:
                result.append(loop.run_until_complete(coro))
            except BaseException as exc:
                error.append(exc)
            finally:
                loop.close()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join(timeout=10)
        if error:
            raise error[0]
        return result[0] if result else None

    def _quota_snap(self, resource_id: str) -> Any:
        """Sync-friendly snapshot that works with both sync and async backends.

        When inside a running event loop (pytest-asyncio), spawns a helper
        thread with its own event loop so we can ``await`` coroutine snapshots
        without triggering *RuntimeError: This event loop is already running*.
        """
        snap = self.quota_reservations.snapshot  # type: ignore[union-attr]
        if asyncio.iscoroutinefunction(snap):
            return self._run_coro_sync(snap(resource_id))
        return snap(resource_id)

    def _apply_policy_constraints(
        self,
        candidates: list[ResourceCandidate],
        route_name: str,
        *,
        is_fallback: bool,
    ) -> list[ResourceCandidate]:
        """Lọc hard policy constraints khi Smart Scheduler đang active.

        Scheduler disabled/shadow giữ nguyên legacy behavior.  Metadata thiếu
        thì filter fail-open, trừ paid resource thiếu giá trong fallback.
        """
        if not self._should_apply_smart_scoring(route_name):
            return candidates
        try:
            preset = self._scoring_config.effective_preset_for_route(route_name)  # type: ignore[union-attr]
            return filter_candidates_for_policy(candidates, preset, is_fallback=is_fallback)
        except Exception:
            return candidates  # Policy lỗi không được làm hỏng data plane

    def select_candidates(self, route_name: str, conversation_thread: str | None = None) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []
        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]
        primary = self._apply_policy_constraints(primary, route_name, is_fallback=False)
        fallback = self._apply_policy_constraints(fallback, route_name, is_fallback=True)
        scored_primary = self._quota_rank_sync(primary)
        scored_fallback = self._quota_rank_sync(fallback)
        return list(self._apply_smart_scoring(scored_primary, route_name, conversation_thread=conversation_thread)) + list(
            self._apply_smart_scoring(scored_fallback, route_name, conversation_thread=conversation_thread)
        )

    async def select_candidates_async(self, route_name: str, conversation_thread: str | None = None) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []

        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]
        primary = self._apply_policy_constraints(primary, route_name, is_fallback=False)
        fallback = self._apply_policy_constraints(fallback, route_name, is_fallback=True)

        scored_primary = await self._quota_rank(primary)
        scored_fallback = await self._quota_rank(fallback)
        return list(self._apply_smart_scoring(scored_primary, route_name, conversation_thread=conversation_thread)) + list(
            self._apply_smart_scoring(scored_fallback, route_name, conversation_thread=conversation_thread)
        )

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
            return list(self._apply_smart_scoring(primary, route_name)) + list(self._apply_smart_scoring(fallback, route_name))

        return self._run_coro_sync(self.select_candidates_async(route_name))

    def _quota_rank_sync(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        """Sync entry-point that delegates to async ``_quota_rank`` via a short-lived loop."""
        if self.quota_reservations is None:
            return candidates
        return list(self._run_coro_sync(self._quota_rank(candidates)))

    async def _quota_rank(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        if self.quota_reservations is None:
            return candidates

        quota_graph = await self._build_quota_graph()
        ranked: list[tuple[int, int, int, ResourceCandidate]] = []
        for index, candidate in enumerate(candidates):
            quota_candidate = await self._check_candidate_quota(candidate, quota_graph=quota_graph)
            if quota_candidate is None:
                continue
            pressure = sum(
                quota_candidate.metadata.get("quota_soft_pressure_by_resource", {}).values()
            )
            remaining_values = list(
                quota_candidate.metadata.get("quota_remaining_by_resource", {}).values()
            )
            min_remaining = min(remaining_values) if remaining_values else None
            low_quota_pressure = 1 if min_remaining is not None and min_remaining <= 1 else 0
            ranked.append((pressure, low_quota_pressure, index, quota_candidate))

        return [candidate for _, _, _, candidate in sorted(ranked, key=lambda item: (item[0], item[1], item[2]))]

    async def _build_quota_graph(self) -> Any | None:
        if self.quota_reservations is None or not hasattr(self.quota_reservations, "list_resources"):
            return None
        try:
            from apps.gateway.quota.graph import QuotaGraph

            graph = QuotaGraph(self.quota_reservations)
            await graph.load()
            return graph
        except Exception:
            return None

    async def _check_candidate_quota(self, candidate: ResourceCandidate, *, quota_graph: Any | None = None) -> ResourceCandidate | None:
        from apps.gateway.quota.reservations import QuotaReservationRequest

        resource_ids = self._known_quota_resource_ids(
            self._quota_resource_ids(candidate),
            quota_graph=quota_graph,
        )
        if not resource_ids:
            return candidate

        requests = [
            QuotaReservationRequest(resource_id, amount=1)
            for resource_id in resource_ids
        ]
        if quota_graph is not None:
            admission = quota_graph.check_many(requests)
        else:
            admission = await self.quota_reservations.check_many(requests)

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

    def _known_quota_resource_ids(self, resource_ids: list[str], *, quota_graph: Any | None = None) -> list[str]:
        known: list[str] = []
        seen_groups: set[str] = set()
        for resource_id in resource_ids:
            try:
                resource = quota_graph.get_resource(resource_id) if quota_graph is not None else self._quota_snap(resource_id)
            except (KeyError, TypeError, ValueError):
                continue
            if resource.metric != "requests":
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

    def _should_apply_smart_scoring(self, route_name: str) -> bool:
        """Return True when rollout config allows scoring for this route."""
        if self._scoring_config is None or not self._scoring_config.enabled:
            return False
        if self._scoring_config.mode not in {"shadow", "active"}:
            return False
        allowlist = getattr(self._scoring_config, "route_allowlist", [])
        return not allowlist or route_name in allowlist

    def _apply_smart_scoring(
        self,
        candidates: list[ResourceCandidate],
        route_name: str,
        conversation_thread: str | None = None,
    ) -> list[ResourceCandidate]:
        """Re-order candidates via smart scoring if enabled and calculator exists."""
        if self._score_calculator is None or not candidates or not self._should_apply_smart_scoring(route_name):
            return candidates
        try:
            keys = [c.resource_ref.provider_connection_id + ":" + c.resource_ref.model_id for c in candidates]
            # Build minimal metrics dict from catalog/quota data
            from apps.gateway.routing.scoring import CandidateMetrics
            metrics: dict[str, CandidateMetrics] = {}
            prices = getattr(self.snapshot, "_prices", {}) or {}
            for candidate, key in zip(candidates, keys):
                model_part = key.split(":", 1)[-1]
                price_info = prices.get(model_part, {})
                eff_remaining = 0
                lim = 0
                sb = 0
                burn_urgency = 0.0
                # Try quota snapshot for each candidate
                rid = f"model:{model_part}"
                if self.quota_reservations:
                    try:
                        res = self._quota_snap(rid)
                        eff_remaining = getattr(res, "effective_remaining", 0)
                        lim = getattr(res, "limit", 0)
                        sb = getattr(res, "safety_buffer", 0)
                    except (KeyError, TypeError, ValueError):
                        pass
                if lim > 0 and eff_remaining >= 0:
                    burn_urgency = 1.0 - (eff_remaining / lim)
                metrics[key] = CandidateMetrics(
                    price_per_million_output=price_info.get("output_per_million"),
                    effective_remaining=eff_remaining,
                    limit=lim,
                    safety_buffer=sb,
                    burn_rate_urgency=burn_urgency,
                    expiry_urgency=float(candidate.metadata.get("expiry_urgency", 0.0)),
                    scarcity=float(candidate.metadata.get("scarcity", 0.0)),
                    retry_expected_cost=float(candidate.metadata.get("retry_expected_cost_per_request", 0.0)),
                    uncertainty=float(candidate.metadata.get("uncertainty_score", 0.0)),
                )
            # Lấy weights theo preset của route (không hardcode provider).
            route_weights = None
            if self._scoring_config is not None:
                try:
                    route_weights = self._scoring_config.effective_weights_for_route(route_name)
                except Exception:
                    route_weights = None
            scored = self._score_calculator.compute_scores(
                candidates=candidates,
                candidate_keys=keys,
                metrics_by_key=metrics,
                conversation_thread=conversation_thread,
                weights=route_weights,
            )
            if self._scoring_config and self._scoring_config.mode == "shadow":
                return candidates
            return [c for c, _ in scored]
        except Exception as exc:
            return candidates  # Graceful degradation
