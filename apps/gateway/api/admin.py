"""Administrative control-plane API for provider setup, revisions, and ledger queries."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
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


@router.get("/revisions/active")
def get_active_revision() -> dict[str, Any]:
    return _serialize_revision(_ensure_active_revision())


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
    return _serialize_revision(_ensure_active_revision())


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
