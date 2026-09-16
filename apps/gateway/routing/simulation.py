"""Route simulation dry-run cho README §22.3.

Không gọi provider/upstream; chỉ đánh giá eligibility theo policy,
chuẩn hóa features và tính score trên snapshot hiện tại.
Giữ routing provider-agnostic — chỉ đọc metadata/preset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.gateway.config.snapshot import RuntimeConfigSnapshot
from apps.gateway.routing.models import ResourceCandidate
from apps.gateway.routing.presets import (
    PolicyPreset, filter_candidates_for_policy, get_preset_or_default,
)
from apps.gateway.routing.scoring import CandidateMetrics, ScoringConfig, SmartScoreCalculator


@dataclass
class SimulationResult:
    route_name: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_resource: str | None = None
    reason: str | None = None
    preset: str | None = None
    expected_reservation: dict[str, Any] | None = None


def _candidate_key(candidate: ResourceCandidate) -> str:
    ref = candidate.resource_ref
    return f"{ref.provider_connection_id}:{ref.model_id}"


def _eligibility_for_candidate(
    candidate: ResourceCandidate,
    preset: PolicyPreset,
    is_fallback: bool,
    required_capabilities: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    meta = getattr(candidate, "metadata", {}) or {}
    failed: list[str] = []
    constraints = preset.constraints

    if constraints.min_quality > 0:
        qscore = meta.get("quality_score")
        if qscore is not None:
            try:
                if float(qscore) < constraints.min_quality:
                    failed.append(f"min_quality {qscore} < {constraints.min_quality}")
            except (TypeError, ValueError):
                pass

    if not constraints.allow_paid_fallback and is_fallback and bool(meta.get("is_paid", False)):
        failed.append("paid_fallback_not_allowed")

    if constraints.allow_paid_fallback and is_fallback and bool(meta.get("is_paid", False)):
        cost = meta.get("expected_cost_per_request")
        if cost is None:
            failed.append("paid_cost_unknown")
        else:
            ceiled = constraints.max_expected_cost_per_request
            if ceiled is not None:
                try:
                    if float(cost) > ceiled:
                        failed.append(f"max_expected_cost_per_request {cost} > {ceiled}")
                except (TypeError, ValueError):
                    pass

    if constraints.min_quota_headroom > 0:
        rem_by_res = meta.get("quota_remaining_by_resource", {})
        lim_by_res = meta.get("quota_limit_by_resource", {})
        if isinstance(rem_by_res, dict) and isinstance(lim_by_res, dict) and rem_by_res and lim_by_res:
            try:
                ratios: list[float] = []
                for key, rem in rem_by_res.items():
                    lim = lim_by_res.get(key)
                    if isinstance(lim, (int, float)) and lim > 0:
                        ratios.append(float(rem) / float(lim))
                if ratios and min(ratios) < constraints.min_quota_headroom:
                    failed.append(f"min_quota_headroom {min(ratios):.4f} < {constraints.min_quota_headroom}")
            except Exception:
                pass

    # Capability eligibility: reuse filter_candidates_for_policy to stay consistent
    caps = required_capabilities if isinstance(required_capabilities, dict) else {}
    if caps and any(v for v in caps.values()):
        try:
            ok = filter_candidates_for_policy(
                [candidate], preset,
                is_fallback=is_fallback,
                required_capabilities=caps,
            )
            if not ok:
                failed.append("capability_eligibility_failed")
        except Exception:
            pass

    return (len(failed) == 0, failed)



def _dry_run_reservation(candidate: ResourceCandidate, estimated_input_tokens: int | None = None) -> dict[str, Any]:
    """Ước lượng expected reservation — không gọi upstream, không reserve thật."""
    meta = getattr(candidate, "metadata", {}) or {}
    resource_ids: list[str] = []
    if isinstance(meta.get("quota_resource_ids"), (list, tuple)):
        resource_ids = [rid for rid in meta["quota_resource_ids"] if isinstance(rid, str) and rid]
    elif isinstance(meta.get("quota_resource_id"), str) and meta["quota_resource_id"]:
        resource_ids = [meta["quota_resource_id"]]
    if not resource_ids and candidate.resource_ref.model_id:
        resource_ids = [f"model:{candidate.resource_ref.model_id}"]

    caps = meta.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    has_tools = bool(caps.get("tools", meta.get("supports_tools", False)))
    max_ctx = caps.get("max_context_tokens", meta.get("max_context_tokens"))
    try:
        max_ctx = int(max_ctx) if max_ctx else None
    except (TypeError, ValueError):
        max_ctx = None

    if estimated_input_tokens is not None:
        estimated_tokens = int(estimated_input_tokens)
    elif max_ctx is not None:
        estimated_tokens = max_ctx + 2000
    else:
        estimated_tokens = 8000

    return {
        "resource_ids": resource_ids,
        "estimated_tokens_per_request": estimated_tokens,
        "has_tools_capability": has_tools,
        "note": "dry-run estimate only — not reserved",
    }


def simulate_route(
    snapshot: RuntimeConfigSnapshot,
    route_name: str,
    scoring_config: ScoringConfig | None = None,
    estimated_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    capabilities: dict[str, Any] | None = None,
    session_id: str | None = None,
    conversation_thread: str | None = None,
    **_kwargs: Any,
) -> SimulationResult:
    """Chạy dry-run cho route; không gọi upstream, không reserve quota."""
    route = snapshot.routes.get(route_name)
    if route is None:
        return SimulationResult(route_name=route_name, candidates=[], selected_resource=None, reason="unknown_route")

    thread = conversation_thread if conversation_thread is not None else session_id

    preset: PolicyPreset
    if scoring_config is not None:
        try:
            preset = scoring_config.effective_preset_for_route(route_name)  # type: ignore[assignment]
        except Exception:
            preset = get_preset_or_default(scoring_config.preset if scoring_config else "auto-free")
    else:
        preset = get_preset_or_default("auto-free")

    all_candidates: list[tuple[ResourceCandidate, bool]] = []
    for cand in getattr(route, "candidates", []) or []:
        all_candidates.append((cand, False))
    for cand in getattr(route, "fallback", []) or []:
        all_candidates.append((cand, True))

    # Chuẩn bị scoring nếu enabled
    calc: SmartScoreCalculator | None = None
    weights = None
    if scoring_config is not None and scoring_config.enabled:
        try:
            calc = SmartScoreCalculator(config=scoring_config)
            weights = scoring_config.effective_weights_for_route(route_name)
        except Exception:
            calc = None

    # Build metrics cho scoring (fail-open khi thiếu telemetry)
    keys: list[str] = [_candidate_key(c) for c, _ in all_candidates]
    metrics_by_key: dict[str, CandidateMetrics] = {}
    prices = getattr(snapshot, "_prices", {}) or {}
    if calc is not None:
        for (candidate, _is_fb), key in zip(all_candidates, keys):
            model_part = key.split(":", 1)[-1]
            price_info = prices.get(model_part, {}) if isinstance(prices, dict) else {}
            meta = getattr(candidate, "metadata", {}) or {}
            metrics_by_key[key] = CandidateMetrics(
                price_per_million_output=price_info.get("output_per_million") if isinstance(price_info, dict) else None,
                expiry_urgency=float(meta.get("expiry_urgency", 0.0) or 0.0),
                scarcity=float(meta.get("scarcity", 0.0) or 0.0),
                retry_expected_cost=float(meta.get("retry_expected_cost_per_request", 0.0) or 0.0),
                uncertainty=float(meta.get("uncertainty_score", 0.0) or 0.0),
            )

    # Tính score chi tiết cho từng candidate khi có calc
    score_by_key: dict[str, dict[str, float]] = {}
    features_by_key: dict[str, dict[str, Any]] = {}
    if calc is not None:
        for (candidate, _is_fb), key in zip(all_candidates, keys):
            metrics = metrics_by_key.get(key)
            if metrics is None:
                continue
            try:
                # candidate object cho calculator có thể là string key hoặc Candidate — dùng string để tránh coupling
                detailed = calc._compute_candidate_score(candidate, key, metrics, thread, weights=weights)  # type: ignore[attr-defined]
                score_by_key[key] = detailed
                features_by_key[key] = {
                    "expiry_urgency": metrics.expiry_urgency,
                    "scarcity": metrics.scarcity,
                    "retry_expected_cost": metrics.retry_expected_cost,
                    "uncertainty": metrics.uncertainty,
                    "price_per_million_output": metrics.price_per_million_output,
                    "estimated_input_tokens": estimated_input_tokens,
                    "max_output_tokens": max_output_tokens,
                    "capabilities": capabilities or {},
                }
            except Exception:
                score_by_key[key] = {"composite": 0.0}
                features_by_key[key] = {}

    rows: list[dict[str, Any]] = []
    eligible_keys: list[str] = []
    for (candidate, is_fallback), key in zip(all_candidates, keys):
        eligible, failed = _eligibility_for_candidate(
            candidate, preset, is_fallback, required_capabilities=capabilities,
        )
        if eligible:
            eligible_keys.append(key)
        score = score_by_key.get(key, {"composite": 0.0})
        features = features_by_key.get(key, {"estimated_input_tokens": estimated_input_tokens, "max_output_tokens": max_output_tokens})
        rows.append(
            {
                "candidate_key": key,
                "provider_connection_id": candidate.resource_ref.provider_connection_id,
                "model_id": candidate.resource_ref.model_id,
                "weight": candidate.weight,
                "is_fallback": is_fallback,
                "eligibility": eligible,
                "failed_constraints": failed,
                "score": score,
                "features": features,
                "metadata": dict(getattr(candidate, "metadata", {}) or {}),
                "expected_reservation": _dry_run_reservation(candidate, estimated_input_tokens),
            }
        )

    # Chọn selected = eligible có composite cao nhất; nếu không có scoring thì giữ order gốc
    selected: str | None = None
    if eligible_keys:
        if calc is not None and score_by_key:
            eligible_rows = [r for r in rows if r["eligibility"]]
            eligible_rows.sort(key=lambda r: float(r["score"].get("composite", 0.0)), reverse=True)
            selected = eligible_rows[0]["candidate_key"] if eligible_rows else None
        else:
            selected = eligible_keys[0]

    reason = f"preset={preset.name}; eligible={len(eligible_keys)}/{len(rows)}"
    # Expected reservation: chọn từ candidate được chọn; nếu nhiều candidates thì trả overview
    exp_res: dict[str, Any] | None = None
    if selected:
        sel_row = next((r for r in rows if r["candidate_key"] == selected), None)
        if sel_row and "expected_reservation" in sel_row:
            exp_res = sel_row["expected_reservation"]
    return SimulationResult(
        route_name=route_name,
        candidates=rows,
        selected_resource=selected,
        reason=reason,
        preset=preset.name,
        expected_reservation=exp_res,
    )
