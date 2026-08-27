from dataclasses import replace
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
        self.quota_reservations = quota_reservations
        self._scoring_config = scoring_config
        self._score_calculator: Any | None = None
        if scoring_config and scoring_config.enabled:
            from apps.gateway.routing.scoring import SmartScoreCalculator
            self._score_calculator = SmartScoreCalculator(config=scoring_config)

    def select_candidates(self, route_name: str) -> List[ResourceCandidate]:
        route = self.snapshot.routes.get(route_name)
        if route is None:
            return []

        # Filter available primary candidates
        primary = [c for c in route.candidates if self.circuit_repository.is_available(c.resource_ref)]

        # Filter available fallback candidates
        fallback = [c for c in route.fallback if self.circuit_repository.is_available(c.resource_ref)]

        return self._apply_smart_scoring(self._quota_rank(primary)) + self._apply_smart_scoring(self._quota_rank(fallback))

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

        resource_ids = self._known_quota_resource_ids(self._quota_resource_ids(candidate))
        if not resource_ids:
            return candidate

        admission = self.quota_reservations.check_many([
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
                resource = self.quota_reservations.snapshot(resource_id)
            except KeyError:
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
                        res = self.quota_reservations.snapshot(rid)
                        eff_remaining = getattr(res, "effective_remaining", 0)
                        lim = getattr(res, "limit", 0)
                    except KeyError:
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
