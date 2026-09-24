from dataclasses import replace
import asyncio
import threading
import time
from typing import Any, List, Optional, Protocol, runtime_checkable

from apps.gateway.routing.models import ResourceRef, ResourceCandidate
from apps.gateway.config.snapshot import RuntimeConfigSnapshot
from apps.gateway.routing.scoring import ScoringConfig
from apps.gateway.routing.presets import filter_candidates_for_policy, hard_state_eligible


@runtime_checkable
class CircuitRepository(Protocol):
    """Storage contract for distributed circuit/cooldown state (P0-14).

    Both the in-memory and Redis-backed implementations satisfy this protocol
    so a credential throttled through one gateway instance is hidden from every
    other instance sharing the same authority.
    """

    def is_available(self, ref: ResourceRef) -> bool:
        ...

    def trip(self, ref: ResourceRef, cooldown_seconds: float) -> None:
        ...


class InMemoryCircuitRepository:
    """Process-local fallback used when no Redis authority is configured."""

    def __init__(self):
        self._states = {}

    def _key(self, ref: ResourceRef) -> str:
        return ref.key

    def is_available(self, ref: ResourceRef) -> bool:
        key = self._key(ref)
        cooldown_until = self._states.get(key, 0.0)
        return cooldown_until <= time.monotonic()

    def trip(self, ref: ResourceRef, cooldown_seconds: float) -> None:
        key = self._key(ref)
        self._states[key] = time.monotonic() + cooldown_seconds

    async def is_available_async(self, ref: ResourceRef) -> bool:
        return self.is_available(ref)

    async def trip_async(self, ref: ResourceRef, cooldown_seconds: float) -> None:
        self.trip(ref, cooldown_seconds)


def _run_coroutine_sync(coro: Any) -> Any:
    """Drive an awaitable to completion from synchronous circuit-check code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    box: list[Any] = []
    errors: list[BaseException] = []

    def runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box.append(loop.run_until_complete(coro))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            loop.close()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=10)
    if errors:
        raise errors[0]
    return box[0] if box else None


class RedisCircuitRepository:
    """Distributed circuit state in Redis with expiring cooldown keys (P0-14).

    The value is the absolute wall-clock expiry so every instance interprets the
    same key identically, and the key also carries a TTL so a crashed gateway
    cannot leave a credential tripped forever.  Works with sync or async redis
    clients: when the client returns a coroutine, the sync entry points drive it
    to completion instead of treating the un-awaited coroutine as a value.
    """

    def __init__(self, redis_client: Any, *, prefix: str = "circuit") -> None:
        self._redis = redis_client
        self._prefix = prefix

    def _key(self, ref: ResourceRef) -> str:
        return f"{self._prefix}:{ref.key}"

    @staticmethod
    def _resolve(value: Any) -> Any:
        return _run_coroutine_sync(value) if asyncio.iscoroutine(value) else value

    def _read_available(self, raw: Any) -> bool:
        if raw is None:
            return True
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return float(raw) <= time.time()
        except (TypeError, ValueError):
            # Unreadable state must not strand a resource permanently.
            return True

    def is_available(self, ref: ResourceRef) -> bool:
        return self._read_available(
            self._resolve(self._redis.get(self._key(ref)))
        )

    def trip(self, ref: ResourceRef, cooldown_seconds: float) -> None:
        ttl = max(1, int(cooldown_seconds))
        self._resolve(
            self._redis.set(
                self._key(ref), str(time.time() + cooldown_seconds), ex=ttl
            )
        )

    async def is_available_async(self, ref: ResourceRef) -> bool:
        return self._read_available(await self._redis.get(self._key(ref)))

    async def trip_async(self, ref: ResourceRef, cooldown_seconds: float) -> None:
        ttl = max(1, int(cooldown_seconds))
        await self._redis.set(
            self._key(ref), str(time.time() + cooldown_seconds), ex=ttl
        )

class RouterEngine:
    def __init__(
        self,
        snapshot: RuntimeConfigSnapshot,
        circuit_repository: Optional[CircuitRepository] = None,
        quota_reservations: Optional[Any] = None,
        scoring_config: Optional[ScoringConfig] = None,
        quota_index: Optional[Any] = None,
    ):
        self.snapshot = snapshot
        self.circuit_repository = circuit_repository or InMemoryCircuitRepository()
        self.quota_index = quota_index
        # Auto-wrap sync backends so async methods can await them.
        if quota_reservations is not None and not asyncio.iscoroutinefunction(
            getattr(quota_reservations, "check_many", None),
        ):
            from apps.gateway.quota.adapter import AsyncQuotaFacade
            quota_reservations = AsyncQuotaFacade(quota_reservations)  # type: ignore[assignment]
        self.quota_reservations = quota_reservations
        self._scoring_config = scoring_config
        self._score_calculator: Any | None = None
        self._rr_current: dict[str, dict[str, float]] = {}
        if scoring_config and scoring_config.enabled:
            from apps.gateway.routing.scoring import SmartScoreCalculator
            self._score_calculator = SmartScoreCalculator(config=scoring_config)

    def _apply_policy_constraints(
        self,
        candidates: list[ResourceCandidate],
        route_name: str,
        *,
        is_fallback: bool,
        required_capabilities: dict[str, Any] | None = None,
    ) -> list[ResourceCandidate]:
        """Lọc hard policy constraints khi Smart Scheduler đang active.

        Scheduler disabled/shadow giữ nguyên legacy behavior.  Metadata thiếu
        thì filter fail-open, trừ paid resource thiếu giá trong fallback.
        """
        # Hard resource state luôn chặn — không phụ thuộc smart scheduler (§18.2).
        candidates = [c for c in candidates if hard_state_eligible(c.metadata)]
        if not self._should_apply_smart_scoring(route_name):
            return candidates
        try:
            preset = self._scoring_config.effective_preset_for_route(route_name)  # type: ignore[union-attr]
            return filter_candidates_for_policy(
                candidates, preset, is_fallback=is_fallback,
                required_capabilities=required_capabilities,
            )
        except Exception:
            return candidates  # Policy lỗi không được làm hỏng data plane

    def _hard_state_only(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        return [c for c in candidates if hard_state_eligible(c.metadata)]

    def _weighted_rr_select(self, candidates: list[ResourceCandidate], route_name: str) -> list[ResourceCandidate]:
        """Smooth weighted round-robin selection.

        Each call advances the RR state and returns candidates ordered with
        the selected candidate first, followed by remaining candidates sorted
        by their current weight deficit (descending).
        """
        if len(candidates) < 2:
            return list(candidates)

        current = self._rr_current.setdefault(route_name, {})
        total_weight = sum(c.weight for c in candidates)

        # Increment each candidate's current weight by its weight
        for candidate in candidates:
            key = candidate.resource_ref.key
            current[key] = current.get(key, 0.0) + candidate.weight

        # Select the candidate with highest current weight
        selected = max(candidates, key=lambda c: current.get(c.resource_ref.key, 0.0))
        selected_key = selected.resource_ref.key

        # Decrement selected candidate's current weight by total weight
        current[selected_key] -= total_weight

        # Build ordered list: selected first, then remaining sorted by current weight (descending)
        remaining = [c for c in candidates if c.resource_ref.key != selected_key]
        remaining.sort(key=lambda c: current.get(c.resource_ref.key, 0.0), reverse=True)

        return [selected] + remaining

    async def _circuit_available_async(self, ref: ResourceRef) -> bool:
        """Await shared circuit state on the async request path (P0-14).

        Repositories that expose ``is_available_async`` (Redis authority) run
        without blocking the event loop; plain sync repositories keep working.
        A backend outage fails open so routing is never stranded by telemetry.
        """
        repo = self.circuit_repository
        check = getattr(repo, "is_available_async", None)
        try:
            if callable(check):
                return bool(await check(ref))
            return bool(repo.is_available(ref))
        except Exception:
            self._circuit_backend_failure(ref)
            return True

    def _circuit_backend_failure(self, ref: ResourceRef) -> None:
        import logging

        logging.getLogger("smart-router").warning(
            "circuit backend check failed for %s; failing open", ref.key,
            exc_info=True,
        )

    def select_candidates(
        self,
        route_name: str,
        conversation_thread: str | None = None,
        required_capabilities: dict[str, Any] | None = None,
    ) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []
        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]
        primary = self._apply_policy_constraints(primary, route_name, is_fallback=False, required_capabilities=required_capabilities)
        fallback = self._apply_policy_constraints(fallback, route_name, is_fallback=True, required_capabilities=required_capabilities)
        scored_primary = self._quota_rank_sync(primary)
        scored_fallback = self._quota_rank_sync(fallback)

        # Check strategy before applying smart scoring
        if route.strategy == "smooth_weighted_rr":
            rr_primary = self._weighted_rr_select(scored_primary, route_name)
            return list(rr_primary) + list(scored_fallback)

        return list(self._apply_smart_scoring(scored_primary, route_name, conversation_thread=conversation_thread)) + list(
            self._apply_smart_scoring(scored_fallback, route_name, conversation_thread=conversation_thread)
        )

    async def select_candidates_async(
        self,
        route_name: str,
        conversation_thread: str | None = None,
        required_capabilities: dict[str, Any] | None = None,
    ) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []

        primary = [c for c in route.candidates if await self._circuit_available_async(c.resource_ref)]
        fallback = [c for c in route.fallback if await self._circuit_available_async(c.resource_ref)]
        primary = self._apply_policy_constraints(primary, route_name, is_fallback=False, required_capabilities=required_capabilities)
        fallback = self._apply_policy_constraints(fallback, route_name, is_fallback=True, required_capabilities=required_capabilities)

        scored_primary = await self._quota_rank(primary)
        scored_fallback = await self._quota_rank(fallback)

        # Check strategy before applying smart scoring
        if route.strategy == "smooth_weighted_rr":
            rr_primary = self._weighted_rr_select(scored_primary, route_name)
            return list(rr_primary) + list(scored_fallback)

        return list(self._apply_smart_scoring(scored_primary, route_name, conversation_thread=conversation_thread)) + list(
            self._apply_smart_scoring(scored_fallback, route_name, conversation_thread=conversation_thread)
        )

    def resolve_route(self, route_name: str) -> List[ResourceCandidate]:
        """Return candidates matching criteria (even fallbacks).

        P0-convergence: the sync entry point must not drive an async authority
        via a thread bridge. When no event loop is running, quota reads are
        served from the local runtime index only; callers that need Redis-backed
        admission must use ``select_candidates_async``.
        """
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []
        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]
        return list(self._quota_rank_sync(primary)) + list(self._quota_rank_sync(fallback))

    def _quota_rank_sync(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        """Sync quota ranking that never touches the async authority."""
        if self.quota_reservations is None:
            return candidates
        return list(self._quota_rank_sync_local(candidates))

    def _quota_rank_sync_local(self, candidates: list[ResourceCandidate]) -> list[ResourceCandidate]:
        if self.quota_index is None or not getattr(self.quota_index, "loaded", False):
            return candidates
        ranked: list[tuple[int, int, int, ResourceCandidate]] = []
        for index, candidate in enumerate(candidates):
            quota_candidate = self._check_candidate_quota_sync(candidate)
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

    def _check_candidate_quota_sync(self, candidate: ResourceCandidate) -> ResourceCandidate | None:
        from apps.gateway.quota.reservations import QuotaReservationRequest

        resource_ids = self._known_quota_resource_ids(
            self._quota_resource_ids(candidate),
            quota_graph=self.quota_index,
        )
        if not resource_ids:
            return candidate
        requests = [
            QuotaReservationRequest(resource_id, amount=1)
            for resource_id in resource_ids
        ]
        admission = self.quota_index.check_many(requests)
        if not admission.accepted:
            return None
        metadata = {
            **candidate.metadata,
            "quota_remaining_by_resource": admission.remaining_by_resource,
        }
        if self._has_valid_quota_resource_ids(candidate):
            metadata["quota_resource_ids"] = self._preserved_quota_resource_ids(
                candidate, resource_ids, quota_graph=self.quota_index
            )
        else:
            metadata["quota_resource_id"] = resource_ids[0]
        if admission.soft_pressure_by_resource:
            metadata["quota_soft_pressure_by_resource"] = admission.soft_pressure_by_resource
        return replace(candidate, metadata=metadata)

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
        """Return the local runtime index for request-time admission reads.

        P0-08: the request path must never rebuild a graph from
        ``store.list_resources()`` (Redis ``SCAN quota:*``).  Index hydration
        belongs to startup/background refresh; when the index is not loaded we
        degrade to targeted per-request ``check_many`` on the authority, which
        scales with the route's candidates, not with every stored resource.
        """
        if self.quota_index is not None and getattr(self.quota_index, "loaded", False):
            return self.quota_index
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
            # Preserve every declared quota dimension for downstream atomic reservation.
            # `resource_ids` may contain only request-metric IDs used for admission;
            # dropping token/budget IDs here would violate multidimensional quota semantics.
            # Keep one request-metric ID per shared group (admission order),
            # plus every known non-request dimension (tokens/budget) that the
            # downstream atomic reservation still needs.
            metadata["quota_resource_ids"] = self._preserved_quota_resource_ids(
                candidate, resource_ids, quota_graph=quota_graph
            )
        else:
            metadata["quota_resource_id"] = resource_ids[0]
        if admission.soft_pressure_by_resource:
            metadata["quota_soft_pressure_by_resource"] = admission.soft_pressure_by_resource

        return replace(candidate, metadata=metadata)

    def _preserved_quota_resource_ids(
        self,
        candidate: ResourceCandidate,
        admission_ids: list[str],
        *,
        quota_graph: Any | None = None,
    ) -> list[str]:
        """Merge deduplicated request-metric IDs with other quota dimensions.

        ``admission_ids`` already holds at most one request-metric resource per
        shared group (P0-07 dedup).  Re-add declared non-request dimensions
        (tokens/budget) so multi-resource reconcile keeps every metric, but drop
        duplicate request-metric IDs that were collapsed during admission.
        """
        preserved = list(admission_ids)
        if quota_graph is None:
            return preserved
        for resource_id in candidate.metadata["quota_resource_ids"]:
            if resource_id in preserved:
                continue
            try:
                resource = quota_graph.get_resource(resource_id)
            except (KeyError, TypeError, ValueError):
                continue
            if resource.metric != "requests":
                preserved.append(resource_id)
        return preserved

    def _known_quota_resource_ids(self, resource_ids: list[str], *, quota_graph: Any | None = None) -> list[str]:
        if quota_graph is None:
            return list(resource_ids)
        known: list[str] = []
        seen_groups: set[str] = set()
        for resource_id in resource_ids:
            try:
                resource = quota_graph.get_resource(resource_id)
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
            keys = [c.resource_ref.key for c in candidates]
            # Build minimal metrics dict from catalog/quota data
            from apps.gateway.routing.scoring import CandidateMetrics
            metrics: dict[str, CandidateMetrics] = {}
            prices = getattr(self.snapshot, "_prices", {}) or {}
            for candidate, key in zip(candidates, keys):
                model_part = key.rsplit(":", 1)[-1]
                price_info = prices.get(model_part, {})
                eff_remaining = 0
                lim = 0
                sb = 0
                burn_urgency = 0.0
                normalized_pressure = 0.0
                # Try quota snapshot for each candidate
                rid = f"model:{model_part}"
                if self.quota_reservations:
                    try:
                        # Prefer local index over per-candidate Redis fetch
                        if self.quota_index is not None and getattr(self.quota_index, "loaded", False):
                            res = self.quota_index.get_resource(rid)
                        else:
                            res = self._quota_snap(rid)
                        eff_remaining = getattr(res, "effective_remaining", 0)
                        lim = getattr(res, "limit", 0)
                        sb = getattr(res, "safety_buffer", 0)
                        if lim > 0:
                            normalized_pressure = 1.0 - (eff_remaining / lim)
                    except (KeyError, TypeError, ValueError):
                        pass
                if lim > 0 and eff_remaining >= 0:
                    burn_urgency = normalized_pressure
                metrics[key] = CandidateMetrics(
                    price_per_million_output=price_info.get("output_per_million"),
                    effective_remaining=eff_remaining,
                    limit=lim,
                    safety_buffer=sb,
                    burn_rate_urgency=burn_urgency,
                    normalized_pressure=normalized_pressure,
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
