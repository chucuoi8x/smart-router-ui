"""Policy presets cho Smart Scheduler theo README section 19 / AC-09.

Mỗi preset là một bộ constraints + weights + retry + reservation mô tả
ưu tiên của từng loại route. Preset chỉ là dữ liệu khai báo, không chứa
logic điều kiện theo tên provider — RoutingEngine/SmartRouter sẽ đọc preset
để quyết định trọng số scoring và ngưỡng kiểm tra, giữ routing core agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
import copy


@dataclass(frozen=True)
class PolicyConstraints:
    """Ngưỡng kiểm tra trước khi cho phép routing."""

    min_quality: float = 0.0  # điểm quality tối thiểu (0-1)
    require_capabilities: str = "auto"  # auto | strict | none
    allow_paid_fallback: bool = False
    max_expected_cost_per_request: float | None = None
    min_quota_headroom: float = 0.0  # phần remaining tối thiểu (0-1)


@dataclass(frozen=True)
class PolicyWeights:
    """Trọng số scoring theo từng preset. Dương = ưu tiên, âm = phạt."""

    free_savings: float = 0.0
    quality_fit: float = 0.0
    reliability: float = 0.0
    quota_headroom: float = 0.0
    expiry_urgency: float = 0.0
    cache_locality: float = 0.0
    latency: float = 0.0
    retry_cost: float = 0.0
    scarcity: float = 0.0
    uncertainty: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    max_extra_input_tokens: int = 60000
    max_extra_latency_ms: int = 5000
    retryable_errors: tuple[str, ...] = (
        "RATE_LIMIT",
        "OVERLOADED",
        "TRANSIENT_NETWORK",
        # Quota hết ở resource hiện tại: failover candidate kế tiếp, không retry cùng resource.
        "QUOTA_EXHAUSTED",
    )


@dataclass(frozen=True)
class ReservationPolicy:
    safety_buffer_ratio: float = 0.05


@dataclass(frozen=True)
class PolicyPreset:
    """Một preset hoàn chỉnh: constraints + weights + retry + reservation."""

    name: str
    description: str = ""
    constraints: PolicyConstraints = field(default_factory=PolicyConstraints)
    weights: PolicyWeights = field(default_factory=PolicyWeights)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    reservation: ReservationPolicy = field(default_factory=ReservationPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "constraints": asdict(self.constraints),
            "weights": self.weights.to_dict(),
            "retry": asdict(self.retry),
            "reservation": asdict(self.reservation),
        }


# ── Định nghĩa 5 preset theo README §19 ──────────────────────────────────


_PRESETS: dict[str, PolicyPreset] = {
    "auto-free": PolicyPreset(
        name="auto-free",
        description="Ưu tiên dung lượng miễn phí hợp lệ, giữ ngưỡng quality tối thiểu.",
        constraints=PolicyConstraints(
            min_quality=0.65,
            require_capabilities="auto",
            allow_paid_fallback=True,
            max_expected_cost_per_request=0.10,
            min_quota_headroom=0.03,
        ),
        weights=PolicyWeights(
            free_savings=1.00,
            quality_fit=0.70,
            reliability=0.55,
            quota_headroom=0.45,
            expiry_urgency=0.35,
            cache_locality=0.30,
            latency=-0.20,
            retry_cost=-0.45,
            scarcity=-0.35,
            uncertainty=-0.25,
        ),
        retry=RetryPolicy(max_attempts=3, max_extra_input_tokens=60000),
        reservation=ReservationPolicy(safety_buffer_ratio=0.05),
    ),
    "fast": PolicyPreset(
        name="fast",
        description="Ưu tiên độ trễ thấp và độ tin cậy, chấp nhận quality vừa đủ.",
        constraints=PolicyConstraints(
            min_quality=0.50,
            require_capabilities="auto",
            allow_paid_fallback=False,
            max_expected_cost_per_request=None,
            min_quota_headroom=0.02,
        ),
        weights=PolicyWeights(
            free_savings=0.20,
            quality_fit=0.40,
            reliability=0.60,
            quota_headroom=0.20,
            expiry_urgency=0.10,
            cache_locality=0.15,
            latency=1.00,
            retry_cost=-0.30,
            scarcity=-0.20,
            uncertainty=-0.15,
        ),
        retry=RetryPolicy(max_attempts=2, max_extra_input_tokens=40000),
        reservation=ReservationPolicy(safety_buffer_ratio=0.03),
    ),
    "coding": PolicyPreset(
        name="coding",
        description="Ưu tiên quality coding, hỗ trợ tool/context và locality.",
        constraints=PolicyConstraints(
            min_quality=0.70,
            require_capabilities="auto",
            allow_paid_fallback=False,
            max_expected_cost_per_request=None,
            min_quota_headroom=0.04,
        ),
        weights=PolicyWeights(
            free_savings=0.40,
            quality_fit=0.90,
            reliability=0.60,
            quota_headroom=0.30,
            expiry_urgency=0.20,
            cache_locality=0.70,
            latency=0.10,
            retry_cost=-0.35,
            scarcity=-0.25,
            uncertainty=-0.20,
        ),
        retry=RetryPolicy(max_attempts=3, max_extra_input_tokens=80000),
        reservation=ReservationPolicy(safety_buffer_ratio=0.05),
    ),
    "review": PolicyPreset(
        name="review",
        description="Ưu tiên reasoning/quality và reliability, tiết kiệm retry với prompt lớn.",
        constraints=PolicyConstraints(
            min_quality=0.75,
            require_capabilities="auto",
            allow_paid_fallback=False,
            max_expected_cost_per_request=None,
            min_quota_headroom=0.05,
        ),
        weights=PolicyWeights(
            free_savings=0.15,
            quality_fit=1.00,
            reliability=0.80,
            quota_headroom=0.25,
            expiry_urgency=0.15,
            cache_locality=0.30,
            latency=-0.10,
            retry_cost=-0.60,
            scarcity=-0.40,
            uncertainty=-0.30,
        ),
        retry=RetryPolicy(max_attempts=2, max_extra_input_tokens=30000),
        reservation=ReservationPolicy(safety_buffer_ratio=0.07),
    ),
    "critical": PolicyPreset(
        name="critical",
        description="Ưu tiên quality và reliability cao nhất, vẫn tôn trọng trần chi phí.",
        constraints=PolicyConstraints(
            min_quality=0.80,
            require_capabilities="strict",
            allow_paid_fallback=True,
            max_expected_cost_per_request=0.50,
            min_quota_headroom=0.05,
        ),
        weights=PolicyWeights(
            free_savings=0.10,
            quality_fit=1.00,
            reliability=1.00,
            quota_headroom=0.30,
            expiry_urgency=0.20,
            cache_locality=0.20,
            latency=0.20,
            retry_cost=-0.30,
            scarcity=-0.20,
            uncertainty=-0.25,
        ),
        retry=RetryPolicy(max_attempts=4, max_extra_input_tokens=100000),
        reservation=ReservationPolicy(safety_buffer_ratio=0.08),
    ),
}


def list_presets() -> list[str]:
    """Trả về danh sách tên preset có sẵn."""
    return sorted(_PRESETS.keys())


def get_preset(name: str) -> PolicyPreset | None:
    """Lấy preset theo tên. Trả về None nếu không tồn tại."""
    return _PRESETS.get(name)


def get_preset_or_default(name: str | None) -> PolicyPreset:
    """Lấy preset theo tên, fallback về auto-free nếu rỗng/không tồn tại."""
    if name and name in _PRESETS:
        return _PRESETS[name]
    return _PRESETS["auto-free"]


def preset_to_scoring_weights(preset: PolicyPreset) -> dict[str, float]:
    """Chuyển weights của preset sang dạng ScoringWeights nếu có thể.

    Chỉ map các chiều đã có trong ScoringWeights hiện tại; các chiều mới
    (free_savings, expiry_urgency, scarcity, retry_cost, uncertainty)
    được map sang 4 factor mới để scheduler phản ánh đúng policy preset.
    """
    w = preset.weights
    # Map sang 10 chiều của ScoringWeights (6 cũ + 4 mới)
    mapped: dict[str, float] = {
        "cost_factor": max(0.0, w.free_savings) if w.free_savings else 0.25,
        "reliability_factor": max(0.0, w.reliability) if w.reliability else 0.30,
        "quota_pressure_factor": max(0.0, w.quota_headroom) if w.quota_headroom else 0.15,
        "capability_factor": max(0.0, w.quality_fit) if w.quality_fit else 0.10,
        "session_affinity_factor": max(0.0, w.cache_locality) if w.cache_locality else 0.10,
        "latency_factor": max(0.0, w.latency) if w.latency and w.latency > 0 else 0.10,
        "expiry_urgency_factor": max(0.0, w.expiry_urgency) if w.expiry_urgency else 0.0,
        "scarcity_factor": max(0.0, abs(w.scarcity)) if w.scarcity else 0.0,
        "retry_cost_factor": max(0.0, abs(w.retry_cost)) if w.retry_cost else 0.0,
        "uncertainty_factor": max(0.0, abs(w.uncertainty)) if w.uncertainty else 0.0,
    }
    # Chuẩn hóa về tổng 1.0 nếu cần; giữ 4 factor mới có trọng số khi preset yêu cầu
    total = sum(mapped.values())
    if total > 0 and abs(total - 1.0) > 0.01:
        inv = 1.0 / total
        mapped = {k: round(v * inv, 4) for k, v in mapped.items()}
    return mapped


def all_presets_dict() -> dict[str, dict[str, Any]]:
    """Trả về toàn bộ presets dạng dict để dùng cho API/UI."""
    return {name: preset.to_dict() for name, preset in _PRESETS.items()}


def _raw_meta_lookup(meta: dict, keys) -> object:
    caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    for key in keys:
        if key in meta and meta[key] is not None:
            return meta[key]
        if isinstance(caps, dict) and key in caps and caps[key] is not None:
            return caps[key]
    return None


def concurrency_state(meta: dict) -> tuple[int, int] | None:
    """Đọc (used, limit) concurrency từ metadata; trả None khi thiếu telemetry (fail-open)."""
    used = _raw_meta_lookup(meta, ("concurrency_used", "concurrent_requests", "inflight"))
    limit = _raw_meta_lookup(meta, ("concurrency_limit", "max_concurrency"))
    if used is None or limit is None:
        return None
    try:
        return int(used), int(limit)
    except (TypeError, ValueError):
        return None


def is_concurrency_exhausted(meta: dict) -> bool:
    """True khi concurrency đã bão hòa (used >= limit); thiếu dữ liệu thì False."""
    state = concurrency_state(meta)
    if state is None:
        return False
    used, limit = state
    if limit <= 0:
        # limit 0 = không cho phép concurrency nào -> coi như exhausted
        return True
    return used >= limit


def filter_candidates_for_policy(
    candidates,
    policy: PolicyPreset | PolicyConstraints,
    *,
    is_fallback: bool = False,
    required_capabilities: dict | None = None,
) -> list:
    """Lọc candidate dựa trên policy constraints của preset.

    Nhận cả ``PolicyPreset`` và ``PolicyConstraints`` để caller không phải
    tự bóc tách cấu hình. Không branch theo tên provider; chỉ đọc metadata.
    """
    constraints = policy.constraints if isinstance(policy, PolicyPreset) else policy
    accepted: list = []
    for c in candidates:
        meta = getattr(c, "metadata", {}) or {}

        # 0. Resource/model state: disabled, deprecated, hidden/unavailable không được chạy.
        if meta.get("enabled") is False:
            continue
        if meta.get("deprecated") is True:
            continue
        state = str(meta.get("model_state", meta.get("state", ""))).strip().lower()
        if state in {"deprecated", "hidden", "disabled", "unavailable", "revoked"}:
            continue

        # 1. Quality floor: reject khi quality_score < min_quality và preset yêu cầu
        if constraints.min_quality > 0:
            qscore = meta.get("quality_score")
            if qscore is not None and float(qscore) < constraints.min_quality:
                continue

        # 2. Paid ceiling: nếu policy không cho phép paid fallback, loại paid ở fallback
        if not constraints.allow_paid_fallback and is_fallback:
            if meta.get("is_paid", False):
                continue

        # 3. Cost ceiling: khi allow_paid_fallback=True, loại paid vượt trần hoặc không rõ chi phí
        if constraints.allow_paid_fallback and is_fallback and meta.get("is_paid", False):
            cost = meta.get("expected_cost_per_request")
            if cost is None:
                # Paid nhưng không có dữ liệu giá -> thận trọng loại
                continue
            ceiled = constraints.max_expected_cost_per_request
            if ceiled is not None and float(cost) > ceiled:
                continue

        # 4. Quota headroom: kiểm tra ratio remaining/limit >= min_quota_headroom
        if constraints.min_quota_headroom > 0:
            rem_by_res = meta.get("quota_remaining_by_resource", {})
            lim_by_res = meta.get("quota_limit_by_resource", {})
            if rem_by_res and lim_by_res:
                try:
                    overall_ratio = min(
                        r / l if l > 0 else 0
                        for r, l in zip(rem_by_res.values(), lim_by_res.values())
                    )
                except ZeroDivisionError:
                    overall_ratio = 0
                if overall_ratio < constraints.min_quota_headroom:
                    continue
            # Thiếu data quota → giữ lại (không chặn)

        # 5. Capability eligibility: kiểm tra tools/vision/context theo require_capabilities
        #    - none: không lọc gì
        #    - auto: chỉ lọc khi candidate khai báo rõ là không hỗ trợ (False / thiếu headroom)
        #    - strict: loại khi thiếu metadata hoặc không đủ context window
        if required_capabilities:
            mode = getattr(constraints, "require_capabilities", "auto") or "auto"
            if mode != "none":
                # Lọc các capability yêu cầu là truthy (True / >0)
                caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
                should_reject = False
                for req_key, req_val in required_capabilities.items():
                    if req_val is None or req_val is False or req_val == 0:
                        continue
                    # Token output: yêu cầu min_output_tokens
                    if req_key == "min_output_tokens":
                        try:
                            needed = int(req_val)
                        except (TypeError, ValueError):
                            continue
                        if needed <= 0:
                            continue
                        max_output = caps.get("max_output_tokens") if isinstance(caps, dict) else None
                        if max_output is None:
                            max_output = meta.get("max_output_tokens")
                        if max_output is None:
                            max_output = meta.get("max_output")
                        # Output size chưa được công bố: fail-open, giống auto context behavior.
                        if max_output is None:
                            continue
                        try:
                            if int(max_output) < needed:
                                should_reject = True
                                break
                        except (TypeError, ValueError):
                            if mode == "strict":
                                should_reject = True
                                break
                    # Context window: yêu cầu min_context_tokens
                    elif req_key == "min_context_tokens":
                        try:
                            needed = int(req_val)
                        except (TypeError, ValueError):
                            continue
                        if needed <= 0:
                            continue
                        max_ctx = caps.get("max_context_tokens") if isinstance(caps, dict) else None
                        if max_ctx is None:
                            max_ctx = meta.get("max_context_tokens")
                        if max_ctx is None:
                            max_ctx = meta.get("context_window")
                        if mode == "auto":
                            # Thiếu data → fail-open
                            if max_ctx is None:
                                continue
                            try:
                                if int(max_ctx) < needed:
                                    should_reject = True
                                    break
                            except (TypeError, ValueError):
                                continue
                        elif mode == "strict":
                            if max_ctx is None:
                                should_reject = True
                                break
                            try:
                                if int(max_ctx) < needed:
                                    should_reject = True
                                    break
                            except (TypeError, ValueError):
                                should_reject = True
                                break
                    else:
                        # Generic boolean capability: tools, vision, etc.
                        has = caps.get(req_key) if isinstance(caps, dict) else None
                        # Fallback: metadata flat key cũng được chấp nhận
                        if has is None and req_key in meta:
                            has = meta.get(req_key)
                        if mode == "auto":
                            if has is False:
                                should_reject = True
                                break
                        elif mode == "strict":
                            if has is not True:
                                should_reject = True
                                break
                if should_reject:
                    continue

        # 6. Concurrency capacity: loại khi đã bão hòa (used >= limit); thiếu data thì fail-open
        if is_concurrency_exhausted(meta):
            continue

        accepted.append(c)

    return accepted
