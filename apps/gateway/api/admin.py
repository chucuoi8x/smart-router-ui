"""Administrative control-plane API for provider setup, revisions, and ledger queries."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.gateway.config.revision import ConfigRevisionManager
from apps.gateway.config.templates import ProviderTemplateRegistry
from apps.gateway.db.dependencies import get_session
from apps.gateway.db.models import AttemptLedger, RequestLedger, UsageLedger


ADMIN_AUTH_TOKEN = "Bearer test-admin-key"


def require_admin_auth(authorization: str = Header(default="")) -> None:
    if authorization != ADMIN_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin credential")


router = APIRouter(dependencies=[Depends(require_admin_auth)])

_template_registry = ProviderTemplateRegistry()
_revision_manager = ConfigRevisionManager()
_provider_connections: dict[str, dict[str, Any]] = {}
_audit_events: list[dict[str, Any]] = []


# ── helpers ────────────────────────────────────────────────────────────

def _serialize_revision(revision: dict[str, Any]) -> dict[str, Any]:
    result = dict(revision)
    created_at = result.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at.isoformat()
    activated_at = result.get("activated_at")
    if isinstance(activated_at, datetime):
        result["activated_at"] = activated_at.isoformat()
    return result


def _ensure_active_revision() -> dict[str, Any]:
    active = _revision_manager.get_active_revision()
    if active is None:
        revision_id = _revision_manager.create_draft({"routes": {}, "connections": {}})
        _revision_manager.activate(revision_id)
        active = _revision_manager.get_active_revision()
    if active is None:
        raise HTTPException(status_code=500, detail="active revision unavailable")
    return active


def _load_template(template_id: str) -> dict[str, Any]:
    try:
        return _template_registry.get_template(template_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="provider template not found") from exc


# ── config endpoints ───────────────────────────────────────────────────

@router.get("/templates")
def list_templates() -> dict[str, dict[str, Any]]:
    return {
        template_id: _template_registry.get_template(template_id)
        for template_id in _template_registry.list_templates()
    }


@router.get("/providers")
def list_providers() -> dict[str, object]:
    items = list(_provider_connections.values())
    return {"items": items, "total": len(items)}


@router.delete("/providers/{connection_id}")
def delete_provider(connection_id: str) -> dict[str, object]:
    if connection_id not in _provider_connections:
        raise HTTPException(status_code=404, detail="provider not found")
    removed = _provider_connections.pop(connection_id)
    _audit_events.append(
        {"action": "provider.deleted", "connection_id": connection_id, "name": removed.get("name"), "created_at": datetime.now(UTC).isoformat()}
    )
    return {"connection_id": connection_id, "deleted": True}


@router.get("/providers/{connection_id}")
def get_provider(connection_id: str) -> dict[str, object]:
    if connection_id not in _provider_connections:
        raise HTTPException(status_code=404, detail="provider not found")
    return dict(_provider_connections[connection_id])


@router.post("/providers", status_code=201)
def create_provider(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    template_id = str(payload.get("template_id") or "")
    if not template_id:
        raise HTTPException(status_code=400, detail="template_id is required")
    template = _load_template(template_id)

    name = str(payload.get("name") or template.get("name") or template_id)
    base_url = str(
        payload.get("base_url")
        or template.get("transport", {}).get("base_url")
        or ""
    )
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url is required")

    connection_id = f"conn_{uuid.uuid4().hex[:12]}"
    record = {
        "connection_id": connection_id,
        "template_id": template_id,
        "name": name,
        "base_url": base_url,
        "driver": template.get("driver"),
        "credential_present": bool(payload.get("api_key")),
    }
    _provider_connections[connection_id] = record
    _ensure_active_revision()
    return dict(record)


@router.get("/routes")
def list_routes() -> dict[str, Any]:
    """List routes from active immutable configuration revision."""
    active = _ensure_active_revision()
    snapshot = active.get("snapshot_data") or {}
    routes = snapshot.get("routes") if isinstance(snapshot, dict) else {}
    routes = routes if isinstance(routes, dict) else {}
    items = [
        {"route_id": route_id, "config": copy.deepcopy(config)}
        for route_id, config in routes.items()
    ]
    return {"items": items, "total": len(items), "revision_id": active["revision_id"]}


@router.put("/routes/{route_id}")
def update_route(route_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create and activate a validated revision containing updated route policy."""
    allowed_strategies = {"priority", "weighted", "smart", "failover"}
    strategy = payload.get("strategy")
    if strategy is not None and str(strategy) not in allowed_strategies:
        raise HTTPException(status_code=400, detail="unsupported route strategy")

    active = _ensure_active_revision()
    snapshot = copy.deepcopy(active.get("snapshot_data") or {})
    routes = snapshot.get("routes")
    if not isinstance(routes, dict) or route_id not in routes:
        raise HTTPException(status_code=404, detail="route not found")

    updated = copy.deepcopy(payload)
    routes[route_id] = updated
    snapshot["routes"] = routes
    revision_id = _revision_manager.create_draft(snapshot)
    valid, errors = _revision_manager.validate(revision_id)
    if not valid:
        raise HTTPException(status_code=400, detail=errors)
    _revision_manager.activate(revision_id)
    _audit_events.append(
        {
            "action": "route.updated",
            "route_id": route_id,
            "revision_id": revision_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    return {
        "route_id": route_id,
        "config": updated,
        "revision_id": revision_id,
    }


@router.get("/revisions/active")
def get_active_revision() -> dict[str, Any]:
    return _serialize_revision(_ensure_active_revision())


@router.get("/revisions")
def list_revisions() -> dict[str, Any]:
    items = [_serialize_revision(r) for r in _revision_manager.list_revisions()]
    return {"items": items, "total": len(items)}


@router.post("/revisions", status_code=201)
def create_revision(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    revision_id = _revision_manager.create_draft(payload)
    return {"revision_id": revision_id}


@router.post("/revisions/{revision_id}/activate")
def activate_revision(revision_id: str) -> dict[str, Any]:
    valid, errors = _revision_manager.validate(revision_id)
    if not valid:
        status_code = 404 if errors == ["Revision not found"] else 400
        raise HTTPException(status_code=status_code, detail=errors)
    _revision_manager.activate(revision_id)
    _audit_events.append(
        {
            "action": "revision.activated",
            "revision_id": revision_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    return _serialize_revision(_ensure_active_revision())


@router.get("/audit")
def list_audit(
    action: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List audit events — redacted, no secrets exposed."""
    items: list[dict[str, Any]] = list(reversed(_audit_events))
    if action:
        items = [e for e in items if e.get("action") == action]
    total = len(items)
    paged = items[offset : offset + limit]
    return {"items": paged, "total": total, "limit": limit, "offset": offset}


# ── ledger query endpoints ─────────────────────────────────────────────

async def _dt_range(start: str | None, end: str | None) -> tuple[Any, Any]:
    """Convert ISO-format start/end query params into timezone-aware datetimes."""
    s: Any | None = None
    e: Any | None = None
    if start:
        val = datetime.fromisoformat(start.replace("Z", "+00:00"))
        s = val
    if end:
        val = datetime.fromisoformat(end.replace("Z", "+00:00"))
        e = val
    return s, e


@router.get("/ledger/requests")
async def list_requests(
    db: AsyncSession = Depends(get_session),
    route_id: str | None = Query(default=None),
    logical_model: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List request ledger entries with optional filters."""
    q = select(RequestLedger).order_by(RequestLedger.created_at.desc())
    if route_id:
        q = q.where(RequestLedger.route_id == route_id)
    if logical_model:
        q = q.where(RequestLedger.logical_model == logical_model)
    q = q.limit(limit).offset(offset)
    rows = (await db.execute(q)).scalars().all()

    data = []
    for r in rows:
        meta = r.metadata_ if hasattr(r, "metadata_") else getattr(r, "metadata", {})
        data.append({
            "request_id": r.request_id,
            "route_id": r.route_id,
            "logical_model": r.logical_model,
            "metadata": meta,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    total = len(data)
    return {"items": data, "total": total, "limit": limit, "offset": offset}


@router.get("/ledger/requests/{request_id}")
async def get_request_detail(
    request_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return a single request with its attempts and usage events."""
    req = (
        await db.execute(select(RequestLedger).where(RequestLedger.request_id == request_id))
    ).scalar_one_or_none()
    if req is None:
        raise HTTPException(status_code=404, detail="request not found")

    meta = req.metadata_ if hasattr(req, "metadata_") else getattr(req, "metadata", {})
    result = {
        "request": {
            "request_id": req.request_id,
            "route_id": req.route_id,
            "logical_model": req.logical_model,
            "metadata": meta,
            "created_at": req.created_at.isoformat() if req.created_at else None,
        },
        "attempts": [],
        "usages": [],
    }

    # Fetch attempts
    attempts = (
        await db.execute(
            select(AttemptLedger)
            .where(AttemptLedger.request_id == request_id)
            .order_by(AttemptLedger.created_at.asc())
        )
    ).scalars().all()
    for a in attempts:
        result["attempts"].append({
            "attempt_id": a.attempt_id,
            "provider_id": a.provider_id,
            "model": a.model,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    # Fetch usage events
    usages = (
        await db.execute(
            select(UsageLedger)
            .where(UsageLedger.request_id == request_id)
            .order_by(UsageLedger.created_at.asc())
        )
    ).scalars().all()
    for u in usages:
        result["usages"].append({
            "id": u.id,
            "attempt_id": u.attempt_id,
            "provider_id": u.provider_id,
            "model": u.model,
            "prompt_tokens": u.prompt_tokens,
            "completion_tokens": u.completion_tokens,
            "cached_input_tokens": u.cached_input_tokens,
            "reasoning_tokens": u.reasoning_tokens,
            "total_tokens": u.total_tokens,
            "estimated_cost": u.estimated_cost,
            "currency": u.currency,
            "source": u.source,
            "confidence": u.confidence,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })

    return result


@router.get("/ledger/stats")
async def usage_stats(
    db: AsyncSession = Depends(get_session),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict[str, Any]:
    """Aggregate usage statistics, optionally filtered by time range."""
    dt_start, dt_end = await _dt_range(start, end)

    # Total tokens & cost aggregated
    stmt = (
        select(
            func.sum(UsageLedger.total_tokens).label("total_tokens"),
            func.sum(UsageLedger.prompt_tokens).label("prompt_tokens"),
            func.sum(UsageLedger.completion_tokens).label("completion_tokens"),
            func.sum(UsageLedger.cached_input_tokens).label("cached_tokens"),
            func.sum(UsageLedger.estimated_cost).label("total_cost"),
            func.count(UsageLedger.id).label("event_count"),
        )
    )
    if dt_start or dt_end:
        conditions = []
        if dt_start:
            conditions.append(UsageLedger.created_at >= dt_start)
        if dt_end:
            conditions.append(UsageLedger.created_at <= dt_end)
        stmt = stmt.where(*conditions)

    row = (await db.execute(stmt)).one()

    # Per-provider breakdown
    provider_stmt = (
        select(
            UsageLedger.provider_id,
            func.sum(UsageLedger.total_tokens).label("total_tokens"),
            func.sum(UsageLedger.estimated_cost).label("cost"),
            func.count(UsageLedger.id).label("events"),
        )
    ).group_by(UsageLedger.provider_id)
    if dt_start or dt_end:
        conditions = []
        if dt_start:
            conditions.append(UsageLedger.created_at >= dt_start)
        if dt_end:
            conditions.append(UsageLedger.created_at <= dt_end)
        provider_stmt = provider_stmt.where(*conditions)

    provider_rows = (await db.execute(provider_stmt)).all()

    return {
        "summary": {
            "total_tokens": int(row.total_tokens or 0),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "cached_tokens": int(row.cached_tokens or 0),
            "total_estimated_cost": float(row.total_cost or 0),
            "event_count": int(row.event_count or 0),
        },
        "by_provider": [
            {
                "provider_id": r.provider_id,
                "total_tokens": int(r.total_tokens or 0),
                "estimated_cost": float(r.cost or 0),
                "event_count": int(r.events or 0),
            }
            for r in provider_rows
        ],
    }


@router.get("/overview")
async def control_plane_overview(
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Aggregate Control Plane overview — no secrets exposed (AC-13)."""
    # 1. Provider connections
    providers = {
        "count": len(_provider_connections),
        "items": [
            {
                "connection_id": c["connection_id"],
                "name": c["name"],
                "template_id": c["template_id"],
                "credential_present": c["credential_present"],
            }
            for c in _provider_connections.values()
        ],
    }

    # 2. Active revision
    active = None
    try:
        rev = _ensure_active_revision()
        active = {
            "revision_id": rev["revision_id"],
            "created_at": rev["created_at"].isoformat() if isinstance(rev.get("created_at"), datetime) else None,
        }
    except Exception:
        pass

    # 3. Usage stats (graceful degradation if DB unavailable)
    stats = None
    try:
        stmt = (
            select(
                func.sum(UsageLedger.total_tokens).label("total_tokens"),
                func.count(UsageLedger.id).label("event_count"),
            )
        )
        row = (await db.execute(stmt)).one()
        stats = {
            "total_tokens": int(row.total_tokens or 0),
            "event_count": int(row.event_count or 0),
        }
    except Exception:
        pass  # DB unavailable → leave stats=None

    return {
        "providers": providers,
        "active_revision": active,
        "revisions": {"active": active},
        "usage": stats,
        "status": "ok",
    }


@router.post("/routes/simulate")
async def simulate_route_endpoint(
    request: Request,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Dry-run route simulation theo README §22.3 — không gọi provider."""
    from pathlib import Path

    from apps.gateway.config.compiler import LegacyConfigCompiler
    from apps.gateway.routing.scoring import ScoringConfig
    from apps.gateway.routing.simulation import simulate_route

    route_name = str(payload.get("route") or payload.get("route_name") or payload.get("model") or "").strip()
    if not route_name:
        raise HTTPException(status_code=400, detail="route is required")

    # Ưu tiên snapshot từ router đang chạy; fallback compile config.yaml
    snapshot = None
    scoring_config: ScoringConfig | None = None
    app_router = getattr(request.app.state, "router", None)
    if app_router is not None:
        try:
            # SmartRouter giữ config gốc; compile snapshot tương thích
            cfg = getattr(app_router, "config", None)
            if isinstance(cfg, dict):
                snapshot = LegacyConfigCompiler().compile_dict(cfg)
                scoring_config = getattr(app_router, "_scoring_config", None)
                if scoring_config is None:
                    scoring_config = ScoringConfig.from_dict(cfg.get("smart_scheduler", {}))
        except Exception:
            snapshot = None

    if snapshot is None:
        # Fallback: đọc config.yaml mặc định
        config_path = Path(__file__).resolve().parents[3] / "config.yaml"
        if not config_path.exists():
            raise HTTPException(status_code=404, detail="route not found")
        try:
            snapshot = LegacyConfigCompiler().compile_file(config_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if scoring_config is None:
            import yaml

            try:
                raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                scoring_config = ScoringConfig.from_dict(raw.get("smart_scheduler", {}))
            except Exception:
                scoring_config = ScoringConfig()

    estimated_input_tokens = payload.get("estimated_input_tokens")
    max_output_tokens = payload.get("max_output_tokens")
    session_val = payload.get("session") or payload.get("session_id") or payload.get("conversation_id")

    result = simulate_route(
        snapshot=snapshot,
        route_name=route_name,
        scoring_config=scoring_config,
        estimated_input_tokens=estimated_input_tokens if isinstance(estimated_input_tokens, int) else None,
        max_output_tokens=max_output_tokens if isinstance(max_output_tokens, int) else None,
        capabilities={
            "tools": bool(payload.get("tools", False)),
            "vision": bool(payload.get("vision", False)),
        },
        session_id=str(session_val).strip() if isinstance(session_val, str) and session_val.strip() else None,
    )

    if not result.candidates and result.reason == "unknown_route":
        raise HTTPException(status_code=404, detail="route not found")

    return {
        "route_name": result.route_name,
        "preset": result.preset,
        "candidates": result.candidates,
        "selected_resource": result.selected_resource,
        "reason": result.reason,
        "expected_reservation": result.expected_reservation,
    }
