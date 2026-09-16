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


def filter_candidates_for_policy(
    candidates,
    policy: PolicyPreset | PolicyConstraints,
    *,
    is_fallback: bool = False,
) -> list:
    """Lọc candidate dựa trên policy constraints của preset.

    Nhận cả ``PolicyPreset`` và ``PolicyConstraints`` để caller không phải
    tự bóc tách cấu hình. Không branch theo tên provider; chỉ đọc metadata.
    """
    constraints = policy.constraints if isinstance(policy, PolicyPreset) else policy
    accepted: list = []
    for c in candidates:
        meta = getattr(c, "metadata", {}) or {}

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

        accepted.append(c)

    return accepted
