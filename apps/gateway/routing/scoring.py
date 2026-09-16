"""Multi-dimensional smart scoring for candidate selection.

Computes a composite score (0-1 scale) per candidate across six dimensions:
cost, reliability, latency, quota pressure, capability match, and session
affinity.  Called by ``SmartRouter`` and ``RouterEngine`` to re-order the
already-filtered candidate list before execution.

**Graceful degradation:** ``compute_scores()`` never raises to its caller.
If any scoring dimension encounters corrupted data it returns the original
list in unchanged order — zero overhead when disabled or misconfigured.
"""
from __future__ import annotations

import logging
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Configuration dataclasses ──────────────────────────────────────────


@dataclass(frozen=True)
class ScoringWeights:
    """Configurable weights for the composite score. Sum must equal 1.0."""

    cost_factor: float = 0.25
    reliability_factor: float = 0.30
    latency_factor: float = 0.10
    quota_pressure_factor: float = 0.15
    capability_factor: float = 0.10
    session_affinity_factor: float = 0.10

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScoringWeights":
        if not data:
            return cls()
        return cls(
            cost_factor=float(data.get("cost_factor", cls.cost_factor)),
            reliability_factor=float(data.get("reliability_factor", cls.reliability_factor)),
            latency_factor=float(data.get("latency_factor", cls.latency_factor)),
            quota_pressure_factor=float(data.get("quota_pressure_factor", cls.quota_pressure_factor)),
            capability_factor=float(data.get("capability_factor", cls.capability_factor)),
            session_affinity_factor=float(data.get("session_affinity_factor", cls.session_affinity_factor)),
        )

    def normalize_if_needed(self) -> tuple["ScoringWeights", bool]:
        """Return (weights, was_normalized). Warns if sum != 1.0 and auto-normalizes."""
        total = (
            self.cost_factor
            + self.reliability_factor
            + self.latency_factor
            + self.quota_pressure_factor
            + self.capability_factor
            + self.session_affinity_factor
        )
        if abs(total - 1.0) < 0.01:
            return self, False
        logger.warning(
            "smart-scoring weights sum to %.4f (expected 1.0), normalizing proportionally",
            total,
        )
        inv = 1.0 / total
        normalized = ScoringWeights(
            cost_factor=self.cost_factor * inv,
            reliability_factor=self.reliability_factor * inv,
            latency_factor=self.latency_factor * inv,
            quota_pressure_factor=self.quota_pressure_factor * inv,
            capability_factor=self.capability_factor * inv,
            session_affinity_factor=self.session_affinity_factor * inv,
        )
        return normalized, True

    def to_dict(self) -> dict[str, float]:
        return {
            "cost_factor": round(self.cost_factor, 4),
            "reliability_factor": round(self.reliability_factor, 4),
            "latency_factor": round(self.latency_factor, 4),
            "quota_pressure_factor": round(self.quota_pressure_factor, 4),
            "capability_factor": round(self.capability_factor, 4),
            "session_affinity_factor": round(self.session_affinity_factor, 4),
        }


@dataclass
class ScoringConfig:
    """Top-level configuration for smart scoring, loaded from YAML.

    All fields are mutable so that callers can tweak thresholds at runtime.
    """

    enabled: bool = False
    mode: str = "disabled"  # "disabled" | "shadow" | "active"
    preset: str = "auto-free"  # tên preset theo README section 19 / AC-09
    route_allowlist: list[str] = field(default_factory=list)
    route_presets: dict[str, str] = field(default_factory=dict)  # route_name -> preset_name
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    normalization_window_seconds: int = 300
    max_failure_history: int = 100
    max_latency_history: int = 100
    session_affinity_ttl_seconds: int = 3600
    burn_rate_penalty_threshold: float = 0.8
    retry_cost_enabled: bool = True
    decision_logging: bool = False
    min_requests_for_metrics: int = 5
    fallback_to_weight_on_scoring_failure: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoringConfig":
        """Parse config block from YAML dict (``config['smart_scheduler']``)."""
        enabled = bool(data.get("enabled", False))
        raw_mode = str(data.get("mode", "active" if enabled else "disabled")).lower()
        if raw_mode not in {"disabled", "shadow", "active"}:
            logger.warning("invalid smart_scheduler.mode=%r; defaulting to disabled", raw_mode)
            raw_mode = "disabled"
        raw_allowlist = data.get("route_allowlist", [])
        route_allowlist = [str(r) for r in raw_allowlist] if isinstance(raw_allowlist, list) else []
        # Hỗ trợ preset: đọc từ config hoặc dùng auto-free mặc định. Không hardcode provider.
        raw_preset = str(data.get("preset", "auto-free")).strip().lower()
        raw_route_presets = data.get("route_presets", {})
        route_presets: dict[str, str] = {}
        if isinstance(raw_route_presets, dict):
            for k, v in raw_route_presets.items():
                route_presets[str(k)] = str(v).strip().lower()
        # Nếu có weights tùy chỉnh trong config, ưu tiên weights đó hơn preset
        has_custom_weights = bool(data.get("weights"))
        cfg = cls(
            enabled=enabled and raw_mode != "disabled",
            mode=raw_mode,
            preset=(raw_preset or "auto-free"),
            route_allowlist=route_allowlist,
            route_presets=route_presets,
            weights=ScoringWeights.from_dict(data.get("weights")),
            normalization_window_seconds=int(data.get("normalization_window_seconds", cls.normalization_window_seconds)),
            max_failure_history=int(data.get("max_failure_history", cls.max_failure_history)),
            max_latency_history=int(data.get("max_latency_history", cls.max_latency_history)),
            session_affinity_ttl_seconds=int(data.get("session_affinity_ttl_seconds", cls.session_affinity_ttl_seconds)),
            burn_rate_penalty_threshold=float(data.get("burn_rate_penalty_threshold", cls.burn_rate_penalty_threshold)),
            retry_cost_enabled=bool(data.get("retry_cost_enabled", cls.retry_cost_enabled)),
            decision_logging=bool(data.get("decision_logging", cls.decision_logging)),
            min_requests_for_metrics=int(data.get("min_requests_for_metrics", cls.min_requests_for_metrics)),
            fallback_to_weight_on_scoring_failure=bool(
                data.get("fallback_to_weight_on_scoring_failure", cls.fallback_to_weight_on_scoring_failure)
            ),
        )
        cfg.weights, _ = cfg.weights.normalize_if_needed()
        # Nếu không có custom weights, nạp weights từ preset để scoring dùng ngay
        if not has_custom_weights:
            try:
                from apps.gateway.routing.presets import get_preset_or_default, preset_to_scoring_weights

                preset_obj = get_preset_or_default(cfg.preset)
                mapped = preset_to_scoring_weights(preset_obj)
                cfg.weights = ScoringWeights(
                    cost_factor=mapped.get("cost_factor", cfg.weights.cost_factor),
                    reliability_factor=mapped.get("reliability_factor", cfg.weights.reliability_factor),
                    latency_factor=mapped.get("latency_factor", cfg.weights.latency_factor),
                    quota_pressure_factor=mapped.get("quota_pressure_factor", cfg.weights.quota_pressure_factor),
                    capability_factor=mapped.get("capability_factor", cfg.weights.capability_factor),
                    session_affinity_factor=mapped.get("session_affinity_factor", cfg.weights.session_affinity_factor),
                )
                cfg.weights, _ = cfg.weights.normalize_if_needed()
            except Exception:
                pass  # preset lỗi thì giữ weights mặc định, không chặn khởi động
        return cfg

    def effective_preset_for_route(self, route_name: str | None = None):
        """Trả về PolicyPreset đã resolve cho route cụ thể.

        Ưu tiên: route_presets[route] -> preset chung -> auto-free.
        """
        preset_name = None
        if route_name and route_name in self.route_presets:
            preset_name = self.route_presets[route_name]
        else:
            preset_name = self.preset
        try:
            from apps.gateway.routing.presets import get_preset_or_default

            return get_preset_or_default(preset_name)
        except Exception:
            from apps.gateway.routing.presets import get_preset_or_default

            return get_preset_or_default("auto-free")

    def effective_weights_for_route(self, route_name: str | None = None) -> "ScoringWeights":
        """Trả về weights đã resolve cho route cụ thể.

        Ưu tiên: route_presets[route] -> preset chung -> weights hiện có.
        """
        try:
            preset = self.effective_preset_for_route(route_name)
            from apps.gateway.routing.presets import preset_to_scoring_weights

            mapped = preset_to_scoring_weights(preset)
            w = ScoringWeights(
                cost_factor=mapped.get("cost_factor", self.weights.cost_factor),
                reliability_factor=mapped.get("reliability_factor", self.weights.reliability_factor),
                latency_factor=mapped.get("latency_factor", self.weights.latency_factor),
                quota_pressure_factor=mapped.get("quota_pressure_factor", self.weights.quota_pressure_factor),
                capability_factor=mapped.get("capability_factor", self.weights.capability_factor),
                session_affinity_factor=mapped.get("session_affinity_factor", self.weights.session_affinity_factor),
            )
            w, _ = w.normalize_if_needed()
            return w
        except Exception:
            return self.weights


# ── Metrics dataclass ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CandidateMetrics:
    """Aggregated metrics for a single candidate, computed at selection time."""

    # Cost
    price_per_million_input: float | None = None
    price_per_million_output: float | None = None

    # Reliability
    rolling_failure_rate: float = 0.0
    circuit_breaker_state: str = "closed"          # "closed" | "open" | "half_open"
    consecutive_failures: int = 0
    total_attempts: int = 0
    total_successes: int = 0

    # Latency
    p50_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    mean_latency_ms: float = 0.0
    request_count: int = 0

    # Quota
    effective_remaining: int = 0
    limit: int = 0
    safety_buffer: int = 0
    burn_rate_urgency: float = 0.0

    # Capability
    capability_match: bool = True

    # Session affinity
    previous_candidate_for_thread: str | None = None
    is_previous_candidate: bool = False

    # Raw metadata passed through for decision logging
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateScore:
    """Single-component score and its contribution to the composite."""

    component: str
    raw_value: float
    normalized: float
    weight: float
    weighted_score: float


@dataclass(frozen=True)
class DecisionLog:
    """Audit trail for why a candidate was chosen."""

    request_id: str | None = None
    route_name: str | None = None
    conversation_thread: str | None = None
    candidates_evaluated: int = 0
    scores: dict[str, dict[str, float]] = field(default_factory=dict)
    selected_candidate: str | None = None
    top_scores: list[tuple[str, float]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# ── Thread-safe metric trackers ────────────────────────────────────────


class RollingFailureRateTracker:
    """Tracks rolling success/failure rates per candidate using a deque of outcomes."""

    def __init__(self, max_history: int = 100) -> None:
        self._max_history = max_history
        self._outcomes: dict[str, deque[bool]] = {}
        self._lock = threading.Lock()

    def record(self, key: str, success: bool) -> None:
        with self._lock:
            if key not in self._outcomes:
                self._outcomes[key] = deque(maxlen=self._max_history)
            self._outcomes[key].append(success)

    def failure_rate(self, key: str) -> tuple[float, int, int]:
        """Returns (rolling_failure_rate, total_attempts, total_successes)."""
        with self._lock:
            outcomes = self._outcomes.get(key)
            if not outcomes:
                return (0.0, 0, 0)
            successes = sum(1 for s in outcomes if s)
            total = len(outcomes)
            return (1.0 - successes / total, total, successes)

    def reset_for(self, key: str) -> None:
        with self._lock:
            self._outcomes.pop(key, None)


class LatencyTracker:
    """Tracks sliding-window latency percentiles per candidate."""

    def __init__(self, max_history: int = 100) -> None:
        self._max_history = max_history
        self._latencies: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, key: str, latency_ms: float) -> None:
        with self._lock:
            if key not in self._latencies:
                self._latencies[key] = deque(maxlen=self._max_history)
            self._latencies[key].append(latency_ms)

    def percentiles(self, key: str) -> tuple[float, float, float]:
        """Returns (p50_ms, p99_ms, mean_ms). All zeros if no data."""
        with self._lock:
            values = self._latencies.get(key)
            if not values:
                return (0.0, 0.0, 0.0)
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            p50 = sorted_vals[n // 2]
            p99_idx = min(int(n * 0.99), n - 1)
            p99 = sorted_vals[p99_idx]
            mean = sum(sorted_vals) / n
            return (p50, p99, mean)

    def count(self, key: str) -> int:
        with self._lock:
            values = self._latencies.get(key)
            return len(values) if values else 0


class SessionAffinityStore:
    """Maps conversation_thread → last_chosen_candidate_key with TTL expiry."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def set(self, conversation_thread: str, candidate_key: str) -> None:
        with self._lock:
            self._store[conversation_thread] = (candidate_key, time.monotonic())

    def get(self, conversation_thread: str) -> str | None:
        with self._lock:
            entry = self._store.get(conversation_thread)
            if entry is None:
                return None
            candidate_key, ts = entry
            if time.monotonic() - ts > self._ttl:
                del self._store[conversation_thread]
                return None
            return candidate_key


# ── Normalization helpers ──────────────────────────────────────────────


def _normalize_min_max(value: float, min_val: float, max_val: float) -> float:
    """Linear normalization to [0, 1]. Clamped. Lower value → higher score."""
    if max_val == min_val:
        return 0.5
    normalized = 1.0 - (value - min_val) / (max_val - min_val)
    return max(0.0, min(1.0, normalized))


def _normalize_inverse(value: float, scale: float) -> float:
    """Normalize where smaller is better: score = value / (value + scale). Yields [0, 1]."""
    if value < 0:
        value = 0.0
    return value / (value + scale)


# ── Public API ─────────────────────────────────────────────────────────


class SmartScoreCalculator:
    """Computes composite scores for candidates based on multiple dimensions.

    Thread-safe for read-only access to tracker state.  The public
    ``compute_scores`` method **never raises** to its caller — on any
    error it returns the original candidate list unchanged so that the
    data plane is never blocked by a scoring misconfiguration.
    """

    def __init__(
        self,
        config: ScoringConfig,
        failure_tracker: RollingFailureRateTracker | None = None,
        latency_tracker: LatencyTracker | None = None,
        session_store: SessionAffinityStore | None = None,
        catalog: dict[str, Any] | None = None,
    ) -> None:
        self._config = config
        self._failure_tracker = failure_tracker or RollingFailureRateTracker(config.max_failure_history)
        self._latency_tracker = latency_tracker or LatencyTracker(config.max_latency_history)
        self._session_store = session_store or SessionAffinityStore(config.session_affinity_ttl_seconds)
        self._catalog = catalog or {}
        self._log_lock = threading.Lock()

    @property
    def config(self) -> ScoringConfig:
        return self._config

    @property
    def failure_tracker(self) -> RollingFailureRateTracker:
        return self._failure_tracker

    @property
    def latency_tracker(self) -> LatencyTracker:
        return self._latency_tracker

    @property
    def session_store(self) -> SessionAffinityStore:
        return self._session_store

    def compute_scores(
        self,
        candidates: list[Any],
        candidate_keys: list[str],
        metrics_by_key: dict[str, CandidateMetrics],
        conversation_thread: str | None = None,
        request_id: str | None = None,
        weights: ScoringWeights | None = None,
    ) -> list[tuple[Any, float]]:
        """Score candidates and return sorted (candidate, composite_score) descending.

        Returns the original list with score=0.0 on any unexpected error.
        """
        try:
            scored: list[tuple[Any, float]] = []
            for candidate, key in zip(candidates, candidate_keys):
                metrics = metrics_by_key.get(key)
                if metrics is None:
                    continue
                score_dict = self._compute_candidate_score(candidate, key, metrics, conversation_thread, weights=weights)
                scored.append((candidate, score_dict["composite"]))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored
        except Exception as exc:
            logger.debug("smart scoring error — returning original order: %s", exc)
            return [(c, 0.0) for c in candidates]

    def record_success(self, key: str) -> None:
        """Record a successful outcome (convenience wrapper)."""
        self._failure_tracker.record(key, True)
        self._latency_tracker.record(key, 0.0)  # actual latency recorded separately

    def record_failure(self, key: str) -> None:
        """Record a failed outcome (convenience wrapper)."""
        self._failure_tracker.record(key, False)

    def record_latency(self, key: str, latency_ms: float) -> None:
        """Record a latency measurement (convenience wrapper)."""
        self._latency_tracker.record(key, latency_ms)

    def remember_affinity(self, conversation_thread: str, candidate_key: str) -> None:
        """Remember last-chosen candidate for a conversation thread."""
        self._session_store.set(conversation_thread, candidate_key)

    def _compute_candidate_score(
        self,
        candidate: Any,
        key: str,
        metrics: CandidateMetrics,
        conversation_thread: str | None,
        weights: ScoringWeights | None = None,
    ) -> dict[str, float]:
        """Compute all six dimension scores and the composite for one candidate."""
        w = weights if weights is not None else self._config.weights

        cost = self._score_cost(metrics)
        reliability = self._score_reliability(metrics)
        latency = self._score_latency(metrics)
        quota = self._score_quota_pressure(metrics)
        capability = self._score_capability(metrics)
        affinity = self._score_session_affinity(key, conversation_thread)

        composite = (
            w.cost_factor * cost
            + w.reliability_factor * reliability
            + w.latency_factor * latency
            + w.quota_pressure_factor * quota
            + w.capability_factor * capability
            + w.session_affinity_factor * affinity
        )

        return {
            "cost": round(cost, 4),
            "reliability": round(reliability, 4),
            "latency": round(latency, 4),
            "quota_pressure": round(quota, 4),
            "capability": round(capability, 4),
            "session_affinity": round(affinity, 4),
            "composite": round(composite, 4),
        }

    def _score_cost(self, m: CandidateMetrics) -> float:
        """Lower output price → higher score via inverse scale."""
        output_price = m.price_per_million_output
        if output_price is None or output_price <= 0:
            return 0.5  # Unknown price → neutral
        return _normalize_inverse(output_price, 1.0)

    def _score_reliability(self, m: CandidateMetrics) -> float:
        """CB open → 0.0, otherwise (1-fail_rate) with consecutive penalty."""
        if m.circuit_breaker_state == "open":
            return 0.0

        base = 1.0 - m.rolling_failure_rate
        if m.consecutive_failures >= 3:
            base *= 0.7
        elif m.consecutive_failures >= 1:
            base *= 0.9
        return max(0.0, min(1.0, base))

    def _score_latency(self, m: CandidateMetrics) -> float:
        """Cold-start → 0.5, otherwise inverse scale on mean latency."""
        if m.request_count < self._config.min_requests_for_metrics:
            return 0.5
        if m.mean_latency_ms <= 0:
            return 0.5
        return _normalize_inverse(m.mean_latency_ms, 10000.0)

    def _score_quota_pressure(self, m: CandidateMetrics) -> float:
        """Higher remaining → higher score, with burn-rate multiplicative penalty."""
        if m.limit <= 0:
            return 0.0

        usage_ratio = 1.0 - (m.effective_remaining / m.limit)
        base = 1.0 - usage_ratio

        burn_urgency = m.burn_rate_urgency
        threshold = self._config.burn_rate_penalty_threshold
        if burn_urgency > 0 and threshold > 0:
            # Apply extra penalty only above the threshold ratio
            excess = max(0.0, burn_urgency - threshold)
            burn_penalty = min(excess / 10.0, 0.5)
            base *= (1.0 - burn_penalty)

        return max(0.0, min(1.0, base))

    def _score_capability(self, m: CandidateMetrics) -> float:
        """Binary match pass-through."""
        return 1.0 if m.capability_match else 0.0

    def _score_session_affinity(self, key: str, conversation_thread: str | None) -> float:
        """Bonus for staying with same candidate in a conversation thread."""
        if not conversation_thread:
            return 0.0
        last_candidate = self._session_store.get(conversation_thread)
        return 1.0 if last_candidate == key else 0.0
