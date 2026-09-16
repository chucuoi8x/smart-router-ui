"""Administrative control-plane API for provider setup, revisions, and ledger queries."""

from __future__ import annotations

import copy
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Form, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.gateway.config.revision import ConfigRevisionManager
from apps.gateway.config.templates import ProviderTemplateRegistry
from apps.gateway.db.dependencies import get_session
from apps.gateway.db.models import AttemptLedger, RequestLedger, UsageLedger
from apps.gateway.providers.registry import default_driver_registry
from apps.gateway.providers.base import DriverNotFoundError
from apps.gateway.security.crypto import encrypt_secret


ADMIN_AUTH_TOKEN = "Bearer test-admin-key"


def require_admin_auth(authorization: str = Header(default="")) -> None:
    if authorization != ADMIN_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin credential")


router = APIRouter(dependencies=[Depends(require_admin_auth)])

_template_registry = ProviderTemplateRegistry()
_revision_manager = ConfigRevisionManager()
_provider_connections: dict[str, dict[str, Any]] = {}
_provider_credentials: dict[str, list[dict[str, Any]]] = {}
_projects: dict[str, dict[str, Any]] = {}
_project_keys: dict[str, list[dict[str, Any]]] = {}
_project_budgets: dict[str, dict[str, Any]] = {}
_policies: dict[str, dict[str, Any]] = {"paid-fallback": {"policy": "paid-fallback", "enabled": False, "requires_budget": True, "project_id": None}}
_alerts: list[dict[str, Any]] = []
_quota_resources: dict[str, dict[str, Any]] = {}
_audit_events: list[dict[str, Any]] = []
_settings: dict[str, Any] = {"log_level": "INFO"}
_driver_registry = default_driver_registry()
_provider_health_cache: dict[str, dict[str, Any]] = {}
def _parse_audit_since(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    # Query strings may decode an unescaped '+' as a space; ISO 8601 has no
    # legal spaces, so normalise back to '+' before parsing.
    raw = value.strip().replace(" ", "+")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid since timestamp") from exc




# ── helpers ────────────────────────────────────────────────────────────

def _serialize_credential(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "credential_id": record.get("credential_id"),
        "alias": record.get("alias"),
        "connection_id": record.get("connection_id"),
        "credential_present": True,
    }


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


def _serialize_provider(record: dict[str, Any]) -> dict[str, Any]:
    """Return provider metadata without credential ciphertext or plaintext."""
    return {
        "connection_id": record["connection_id"],
        "template_id": record["template_id"],
        "name": record["name"],
        "base_url": record["base_url"],
        "driver": record["driver"],
        "active": bool(record.get("active", True)),
        "disabled": not bool(record.get("active", True)),
        "credential_present": bool(record.get("credential_present")),
    }


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


@router.get("/version")
def admin_version(request: Request) -> dict[str, Any]:
    return {"service": "smart-router", "version": request.app.version}


@router.get("/providers")
def list_providers() -> dict[str, object]:
    items = [_serialize_provider(record) for record in _provider_connections.values()]
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


@router.get("/providers/health")
def list_providers_health() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    now = datetime.now(UTC).isoformat()
    for record in _provider_connections.values():
        cached = _provider_health_cache.get(record["connection_id"], {})
        items.append({
            "connection_id": record["connection_id"],
            "template_id": record.get("template_id"),
            "name": record.get("name"),
            "base_url": record.get("base_url"),
            "driver": record.get("driver"),
            "status": cached.get("status", "unknown"),
            "checked_at": cached.get("checked_at", now),
            "credential_present": bool(record.get("credential_present")),
        })
    return {"items": items, "total": len(items)}


@router.get("/providers/{connection_id}/health")
def get_provider_health(connection_id: str) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    cached = _provider_health_cache.get(connection_id, {})
    return {
        "connection_id": record["connection_id"],
        "template_id": record.get("template_id"),
        "name": record.get("name"),
        "base_url": record.get("base_url"),
        "driver": record.get("driver"),
        "status": cached.get("status", "unknown"),
        "checked_at": cached.get("checked_at", datetime.now(UTC).isoformat()),
        "credential_present": bool(record.get("credential_present")),
    }


@router.put("/providers/{connection_id}")
def update_provider(connection_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    name = payload.get("name")
    if name is not None:
        cleaned = str(name).strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        record["name"] = cleaned
    base_url = payload.get("base_url")
    if base_url is not None:
        cleaned_url = str(base_url).strip()
        if not cleaned_url:
            raise HTTPException(status_code=400, detail="base_url cannot be empty")
        record["base_url"] = cleaned_url
    driver = payload.get("driver")
    if driver is not None:
        record["driver"] = str(driver)
    api_key = payload.get("api_key")
    if api_key:
        record["credential_present"] = True
        record["credential_encrypted"] = encrypt_secret(str(api_key))
    if "active" in payload:
        record["active"] = bool(payload["active"])
    timestamp = datetime.now(UTC).isoformat()
    _audit_events.append({
        "action": "provider.updated",
        "connection_id": connection_id,
        "fields": sorted([k for k in payload if k != "api_key"]) + (["credential_present"] if api_key else []),
        "created_at": timestamp,
    })
    return _serialize_provider(record)


@router.post("/providers/{connection_id}/deactivate")
def deactivate_provider(connection_id: str) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    record["active"] = False
    timestamp = datetime.now(UTC).isoformat()
    _audit_events.append({"action": "provider.deactivated", "connection_id": connection_id, "created_at": timestamp})
    return {"connection_id": connection_id, "active": False, "disabled": True, "changed_at": timestamp}


@router.post("/providers/{connection_id}/reactivate")
def reactivate_provider(connection_id: str) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    record["active"] = True
    timestamp = datetime.now(UTC).isoformat()
    _audit_events.append({"action": "provider.reactivated", "connection_id": connection_id, "created_at": timestamp})
    return {"connection_id": connection_id, "active": True, "disabled": False, "changed_at": timestamp}


@router.get("/providers/{connection_id}")
def get_provider(connection_id: str) -> dict[str, object]:
    if connection_id not in _provider_connections:
        raise HTTPException(status_code=404, detail="provider not found")
    return _serialize_provider(_provider_connections[connection_id])


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
        "driver": str(payload.get("driver") or template.get("driver") or ""),
        "active": True,
        "credential_present": bool(payload.get("api_key")),
        # Stored only as Fernet ciphertext; serializers below never expose it.
        "credential_encrypted": encrypt_secret(str(payload["api_key"])) if payload.get("api_key") else None,
    }
    _provider_connections[connection_id] = record
    _ensure_active_revision()
    return _serialize_provider(record)


@router.post("/providers/{connection_id}/test")
async def test_provider_connection(connection_id: str) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    driver_id = str(record.get("driver") or record.get("template_id") or "generic-openai")
    try:
        driver_cls = _driver_registry.resolve(driver_id)
    except DriverNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        driver = driver_cls()
        ctx = {
            "connection_id": connection_id,
            "base_url": record.get("base_url"),
            "driver": driver_id,
            "template_id": record.get("template_id"),
        }
        result = await driver.validate_connection(ctx)  # type: ignore[func-returns-value]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    normalized_result = result if isinstance(result, dict) else {"result": result}
    result_status = str(normalized_result.get("status", "ok")).lower()
    health_status = "healthy" if result_status in {"ok", "healthy", "success"} else "degraded"
    _provider_health_cache[connection_id] = {
        "status": health_status,
        "checked_at": datetime.now(UTC).isoformat(),
        "result": {k: v for k, v in normalized_result.items() if k not in {"api_key", "credential", "token", "secret"}},
    }
    return {"ok": True, "connection_id": connection_id, "result": normalized_result}


@router.post("/providers/{connection_id}/discover")
async def discover_provider_models(connection_id: str) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    driver_id = str(record.get("driver") or record.get("template_id") or "generic-openai")
    try:
        driver_cls = _driver_registry.resolve(driver_id)
    except DriverNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        driver = driver_cls()
        ctx = {
            "connection_id": connection_id,
            "base_url": record.get("base_url"),
            "driver": driver_id,
            "template_id": record.get("template_id"),
        }
        models = await driver.discover_models(ctx)  # type: ignore[func-returns-value]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    normalized: list[dict[str, Any]] = []
    for m in models or []:
        if isinstance(m, dict):
            normalized.append(m)
        elif isinstance(m, str):
            normalized.append({"id": m})
        else:
            normalized.append({"id": str(m)})



    return {"connection_id": connection_id, "models": normalized, "count": len(normalized)}


@router.post("/providers/{connection_id}/models/import", status_code=201)
def import_provider_models(connection_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    route_id = str(payload.get("route_id") or "").strip()
    if not route_id:
        raise HTTPException(status_code=400, detail="route_id is required")
    model_list = payload.get("models")
    if not isinstance(model_list, list) or not all(isinstance(m, str) for m in model_list):
        raise HTTPException(status_code=400, detail="models must be a non-empty list of strings")
    new_models = [str(m).strip() for m in model_list if str(m).strip()]
    if not new_models:
        raise HTTPException(status_code=400, detail="models cannot be empty")
    active = _ensure_active_revision()
    snapshot = copy.deepcopy(active.get("snapshot_data") or {})
    routes = snapshot.get("routes") if isinstance(snapshot, dict) else {}
    if not isinstance(routes, dict):
        routes = {}
    route_cfg = routes.get(route_id)
    if route_cfg is None:
        raise HTTPException(status_code=404, detail="route not found")
    candidates = route_cfg.get("candidates") if isinstance(route_cfg, dict) else []
    if not isinstance(candidates, list):
        candidates = []
    seen = {c.get("model") for c in candidates}
    added = []
    for mdl in new_models:
        if mdl not in seen:
            candidates.append({"upstream": connection_id, "model": mdl})
            added.append(mdl)
            seen.add(mdl)
    routes[route_id] = {"strategy": route_cfg.get("strategy","priority"), "candidates": candidates, "fallback": route_cfg.get("fallback",[])}
    snapshot["routes"] = routes
    rev_id = _revision_manager.create_draft(snapshot)
    valid, errs = _revision_manager.validate(rev_id)
    if not valid:
        raise HTTPException(status_code=400, detail=errs)
    _revision_manager.activate(rev_id)
    _audit_events.append({"action":"route.models.imported","route_id":route_id,"provider_id":connection_id,"imported":added,"revision_id":rev_id,"created_at":datetime.now(UTC).isoformat()})
    return {"route_id":route_id,"revision_id":rev_id,"imported":added,"candidate_count":len(candidates)}


@router.post("/providers/{connection_id}/credentials", status_code=201)
def add_provider_credential(connection_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    record = _provider_connections.get(connection_id)
    if record is None:
        raise HTTPException(status_code=404, detail="provider not found")
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    alias = str(payload.get("alias") or payload.get("name") or f"cred_{uuid.uuid4().hex[:8]}").strip()
    cred_id = f"cred_{uuid.uuid4().hex[:12]}"
    cred = {
        "credential_id": cred_id,
        "connection_id": connection_id,
        "alias": alias,
        "credential_encrypted": encrypt_secret(api_key),
    }
    _provider_credentials.setdefault(connection_id, []).append(cred)
    _audit_events.append({"action": "credential.added", "connection_id": connection_id, "credential_id": cred_id, "alias": alias, "created_at": datetime.now(UTC).isoformat()})
    return _serialize_credential(cred)


@router.get("/providers/{connection_id}/credentials")
def list_provider_credentials(connection_id: str) -> dict[str, Any]:
    if connection_id not in _provider_connections:
        raise HTTPException(status_code=404, detail="provider not found")
    items = [_serialize_credential(c) for c in _provider_credentials.get(connection_id, [])]
    return {"items": items, "total": len(items), "connection_id": connection_id}


@router.delete("/providers/{connection_id}/credentials/{credential_id}")
def delete_provider_credential(connection_id: str, credential_id: str) -> dict[str, Any]:
    if connection_id not in _provider_connections:
        raise HTTPException(status_code=404, detail="provider not found")
    creds = _provider_credentials.get(connection_id, [])
    target = None
    for c in creds:
        if c.get("credential_id") == credential_id:
            target = c
            break
    if target is None:
        raise HTTPException(status_code=404, detail="credential not found")
    alias = target.get("alias", "")
    creds.remove(target)
    _provider_credentials[connection_id] = creds
    _audit_events.append({"action": "credential.deleted", "connection_id": connection_id, "credential_id": credential_id, "alias": alias, "created_at": datetime.now(UTC).isoformat()})
    return {"deleted": True, "credential_id": credential_id}


@router.post("/projects", status_code=201)
def create_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    secret_key = f"sr_{uuid.uuid4().hex}"
    record = {
        "project_id": project_id,
        "name": name,
        "description": str(payload.get("description") or ""),
        "key_hash": encrypt_secret(secret_key),
        "created_at": datetime.now(UTC).isoformat(),
        "active": True,
    }
    _projects[project_id] = record
    _audit_events.append({"action":"project.created","project_id":project_id,"name":name,"created_at":datetime.now(UTC).isoformat()})
    # Secret key returned exactly once; never persisted plaintext or returned by list/detail.
    return {"project_id": project_id, "name": name, "description": record["description"], "secret_key": secret_key}


@router.get("/projects")
def list_projects() -> dict[str, Any]:
    items = [
        {"project_id": p["project_id"], "name": p["name"], "description": p["description"], "active": p["active"], "created_at": p["created_at"]}
        for p in _projects.values()
    ]
    return {"items": items, "total": len(items)}


@router.post("/projects/{project_id}/keys", status_code=201)
def create_project_key(project_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    project = _projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    alias = str(payload.get("alias") or "default").strip()
    if not alias:
        raise HTTPException(status_code=400, detail="alias cannot be empty")
    key_id = f"key_{uuid.uuid4().hex[:12]}"
    secret = f"srk_{uuid.uuid4().hex}"
    record = {
        "key_id": key_id,
        "project_id": project_id,
        "alias": alias,
        "secret_encrypted": encrypt_secret(secret),
        "active": True,
        "created_at": datetime.now(UTC).isoformat(),
        "revoked_at": None,
    }
    _project_keys.setdefault(project_id, []).append(record)
    _audit_events.append({"action": "project.key.created", "project_id": project_id, "key_id": key_id, "alias": alias, "created_at": record["created_at"]})
    return {"key_id": key_id, "project_id": project_id, "alias": alias, "secret": secret, "active": True, "created_at": record["created_at"]}


@router.get("/projects/{project_id}/keys")
def list_project_keys(project_id: str) -> dict[str, Any]:
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="project not found")
    items = [
        {"key_id": k["key_id"], "project_id": project_id, "alias": k["alias"], "active": bool(k["active"]), "created_at": k["created_at"], "revoked_at": k.get("revoked_at")}
        for k in _project_keys.get(project_id, [])
    ]
    return {"items": items, "total": len(items)}


@router.delete("/projects/{project_id}/keys/{key_id}", status_code=200)
def revoke_project_key(project_id: str, key_id: str) -> dict[str, Any]:
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="project not found")
    target = next((k for k in _project_keys.get(project_id, []) if k["key_id"] == key_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="project key not found")
    if target["active"]:
        target["active"] = False
        target["revoked_at"] = datetime.now(UTC).isoformat()
        _audit_events.append({"action": "project.key.revoked", "project_id": project_id, "key_id": key_id, "created_at": target["revoked_at"]})
    return {"project_id": project_id, "key_id": key_id, "revoked": True, "active": False, "revoked_at": target["revoked_at"]}


@router.put("/projects/{project_id}/budget")
def upsert_project_budget(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project = _projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    ceiling_raw = payload.get("ceiling")
    if ceiling_raw is None:
        raise HTTPException(status_code=400, detail="ceiling is required")
    try:
        ceiling = float(ceiling_raw)
        used = float(payload.get("used", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="ceiling/used must be numbers") from exc
    if ceiling < 0 or used < 0 or used > ceiling:
        raise HTTPException(status_code=400, detail="invalid budget bounds: used must be between 0 and ceiling")
    currency = str(payload.get("currency") or "USD").strip().upper()
    if not currency:
        currency = "USD"
    allow_paid = bool(payload.get("allow_paid_fallback", True)) if "allow_paid_fallback" in payload else True
    # if explicitly passed allow_paid_fallback keep that value
    if "allow_paid_fallback" in payload:
        allow_paid = bool(payload["allow_paid_fallback"])
    remaining = ceiling - used
    record = {
        "project_id": project_id,
        "currency": currency,
        "ceiling": ceiling,
        "used": used,
        "remaining": remaining,
        "allow_paid_fallback": allow_paid,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _project_budgets[project_id] = record
    _audit_events.append({"action": "project.budget.updated", "project_id": project_id, "ceiling": ceiling, "used": used, "created_at": record["updated_at"]})
    return dict(record)


@router.get("/projects/{project_id}/budget")
def get_project_budget(project_id: str) -> dict[str, Any]:
    project = _projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    record = _project_budgets.get(project_id)
    if record is None:
        raise HTTPException(status_code=404, detail="budget not found")
    return dict(record)


@router.get("/policies/paid-fallback")
def get_paid_fallback_policy() -> dict[str, Any]:
    record = _policies.get("paid-fallback", {"policy": "paid-fallback", "enabled": False, "requires_budget": True, "project_id": None})
    return dict(record)


@router.put("/policies/paid-fallback")
def update_paid_fallback_policy(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="enabled is required")
    enabled = bool(payload["enabled"])
    project_id = payload.get("project_id")
    if enabled:
        # Enabling requires budget approval when a project_id is supplied, or global gate when not.
        if project_id is not None:
            project_id = str(project_id)
            if project_id not in _projects:
                raise HTTPException(status_code=404, detail="project not found")
            budget = _project_budgets.get(project_id)
            if budget is None or not bool(budget.get("allow_paid_fallback")):
                raise HTTPException(status_code=400, detail="paid fallback requires budget with allow_paid_fallback=true")
            remaining = float(budget.get("remaining", budget.get("ceiling", 0)) )
            if remaining <= 0 and float(budget.get("ceiling", 0)) > 0:
                raise HTTPException(status_code=400, detail="budget exhausted: cannot enable paid fallback")
        else:
            # Global enable without project: require at least one budget with allow_paid_fallback=true
            if not any(bool(b.get("allow_paid_fallback")) for b in _project_budgets.values()):
                raise HTTPException(status_code=400, detail="paid fallback requires at least one budget with allow_paid_fallback=true")
    record = {"policy": "paid-fallback", "enabled": enabled, "requires_budget": True, "project_id": project_id}
    _policies["paid-fallback"] = record
    _audit_events.append({"action": "policy.paid_fallback.updated", "enabled": enabled, "project_id": project_id, "created_at": datetime.now(UTC).isoformat()})
    return dict(record)


@router.get("/budgets")
def list_budgets() -> dict[str, Any]:
    items = [dict(v) for v in _project_budgets.values()]
    return {"items": items, "total": len(items)}


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    record = _projects.get(project_id)
    if record is None:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project_id": record["project_id"], "name": record["name"], "description": record["description"], "active": record["active"], "created_at": record["created_at"]}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    record = _projects.pop(project_id, None)
    if record is None:
        raise HTTPException(status_code=404, detail="project not found")
    _audit_events.append({"action":"project.deleted","project_id":project_id,"name":record["name"],"created_at":datetime.now(UTC).isoformat()})
    return {"project_id": project_id, "deleted": True}


@router.post("/alerts", status_code=201)
def create_alert(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    severity = str(payload.get("severity") or "info").strip().lower()
    if severity not in {"info", "warning", "critical"}:
        raise HTTPException(status_code=400, detail="severity must be info, warning or critical")
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    alert = {
        "alert_id": f"alert_{uuid.uuid4().hex[:12]}",
        "severity": severity,
        "message": message,
        "source": str(payload.get("source") or "manual"),
        "status": "open",
        "created_at": datetime.now(UTC).isoformat(),
    }
    _alerts.append(alert)
    _audit_events.append({"action":"alert.created","alert_id":alert["alert_id"],"severity":severity,"created_at":alert["created_at"]})
    return dict(alert)


@router.get("/alerts")
def list_alerts(status: str | None = Query(default=None), severity: str | None = Query(default=None)) -> dict[str, Any]:
    items = list(reversed(_alerts))
    if status:
        items = [a for a in items if a["status"] == status]
    if severity:
        items = [a for a in items if a["severity"] == severity]
    return {"items": items, "total": len(items)}


@router.patch("/alerts/{alert_id}")
def update_alert(alert_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    target = None
    for a in _alerts:
        if a["alert_id"] == alert_id:
            target = a
            break
    if target is None:
        raise HTTPException(status_code=404, detail="alert not found")
    new_status = str(payload.get("status") or "").strip().lower()
    if new_status not in {"open", "acknowledged", "resolved"}:
        raise HTTPException(status_code=400, detail="status must be open, acknowledged or resolved")
    target["status"] = new_status
    _audit_events.append({"action":"alert.updated","alert_id":alert_id,"status":new_status,"created_at":datetime.now(UTC).isoformat()})
    return dict(target)


def _serialize_quota_resource(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["remaining"] = max(0, int(result["limit"]) - int(result.get("used", 0)) - int(result.get("safety_buffer", 0)))
    return result


@router.post("/quota/resources", status_code=201)
def create_quota_resource(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    resource_id = str(payload.get("resource_id") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    metric = str(payload.get("metric") or "").strip()
    if not resource_id or not scope or not metric:
        raise HTTPException(status_code=400, detail="resource_id, scope and metric are required")
    if resource_id in _quota_resources:
        raise HTTPException(status_code=409, detail="quota resource already exists")
    try:
        limit = int(payload.get("limit"))
        window_seconds = int(payload.get("window_seconds"))
        used = int(payload.get("used", 0))
        safety_buffer = int(payload.get("safety_buffer", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="limit/window_seconds/used/safety_buffer must be integers") from exc
    if limit < 0 or window_seconds <= 0 or used < 0 or used > limit or safety_buffer < 0 or safety_buffer > limit:
        raise HTTPException(status_code=400, detail="invalid quota resource bounds")
    record = {
        "resource_id": resource_id, "scope": scope, "metric": metric, "limit": limit,
        "used": used, "window_seconds": window_seconds, "safety_buffer": safety_buffer,
        "hard_limit": bool(payload.get("hard_limit", True)),
        "shared_group_id": payload.get("shared_group_id"),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _quota_resources[resource_id] = record
    _audit_events.append({"action":"quota.resource.created","resource_id":resource_id,"metric":metric,"created_at":record["created_at"]})
    return _serialize_quota_resource(record)


@router.get("/quota/resources")
def list_quota_resources(scope: str | None = Query(default=None), metric: str | None = Query(default=None)) -> dict[str, Any]:
    items = list(_quota_resources.values())
    if scope:
        items = [r for r in items if r["scope"] == scope]
    if metric:
        items = [r for r in items if r["metric"] == metric]
    data = [_serialize_quota_resource(r) for r in items]
    return {"items": data, "total": len(data)}


@router.get("/quota/resources/{resource_id}")
def get_quota_resource(resource_id: str) -> dict[str, Any]:
    record = _quota_resources.get(resource_id)
    if record is None:
        raise HTTPException(status_code=404, detail="quota resource not found")
    return _serialize_quota_resource(record)


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


@router.get("/revisions/{revision_id}")
def get_revision_detail(revision_id: str) -> dict[str, Any]:
    for rev in _revision_manager.list_revisions():
        if rev.get("revision_id") == revision_id:
            return _serialize_revision(rev)
    raise HTTPException(status_code=404, detail="revision not found")


@router.post("/revisions", status_code=201)
def create_revision(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
    revision_id = _revision_manager.create_draft(payload)
    return {"revision_id": revision_id}


@router.post("/revisions/{revision_id}/rollback")
def rollback_revision(revision_id: str) -> dict[str, Any]:
    """Activate a prior immutable revision with an explicit rollback audit event."""
    target = None
    for revision in _revision_manager.list_revisions():
        if revision.get("revision_id") == revision_id:
            target = revision
            break
    if target is None:
        raise HTTPException(status_code=404, detail="revision not found")
    current = _revision_manager.get_active_revision()
    if current is not None and current.get("revision_id") == revision_id:
        raise HTTPException(status_code=400, detail="revision is already active")
    previous_id = current.get("revision_id") if current else None
    valid, errors = _revision_manager.validate(revision_id)
    if not valid:
        raise HTTPException(status_code=400, detail=errors)
    _revision_manager.activate(revision_id)
    _audit_events.append(
        {
            "action": "revision.rolled_back",
            "revision_id": revision_id,
            "from_revision_id": previous_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    result = _serialize_revision(_ensure_active_revision())
    result["rolled_back_from"] = previous_id
    return result


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


@router.get("/audit/export")
def export_audit(
    action: str | None = Query(default=None),
    since: str | None = Query(default=None),
) -> dict[str, Any]:
    """Export audit events as JSON — redacted, never includes plaintext secrets."""
    since_dt = _parse_audit_since(since)
    items: list[dict[str, Any]] = list(reversed(_audit_events))
    if action:
        items = [e for e in items if e.get("action") == action]
    if since_dt is not None:
        def _created_at(e: dict[str, Any]) -> datetime | None:
            val = e.get("created_at")
            if not isinstance(val, str):
                return None
            try:
                dt = datetime.fromisoformat(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except Exception:
                return None
        items = [e for e in items if (_created_at(e) is not None and _created_at(e) >= since_dt)]  # type: ignore[operator]
    # redaction guard: ensure no secret key leaks even if stored incorrectly
    redacted: list[dict[str, Any]] = []
    for event in items:
        safe = {k: v for k, v in event.items() if k.lower() not in {"api_key", "credential_encrypted", "credential", "secret", "token"}}
        # also scrub any value that looks like a secret
        for k, v in list(safe.items()):
            if isinstance(v, str) and v.startswith("sk-"):
                safe[k] = "[REDACTED]"
        redacted.append(safe)
    return {"items": redacted, "total": len(redacted), "exported_at": datetime.now(UTC).isoformat()}


@router.get("/audit")
def list_audit(
    action: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    since: str | None = Query(default=None),
) -> dict[str, Any]:
    """List audit events — redacted, no secrets exposed."""
    since_dt = _parse_audit_since(since)
    items: list[dict[str, Any]] = list(reversed(_audit_events))
    if action:
        items = [e for e in items if e.get("action") == action]
    if since_dt is not None:
        def _created_at(e: dict[str, Any]) -> datetime | None:
            val = e.get("created_at")
            if not isinstance(val, str):
                return None
            try:
                dt = datetime.fromisoformat(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt
            except Exception:
                return None
        items = [e for e in items if (_created_at(e) is not None and _created_at(e) >= since_dt)]  # type: ignore[operator]
    total = len(items)
    paged = items[offset : offset + limit]
    return {"items": paged, "total": total, "limit": limit, "offset": offset}


@router.get("/models")
def list_models(route_id: str | None = Query(default=None)) -> dict[str, Any]:
    """List model resources from active revision — Control Plane view (AC-13)."""
    active = _ensure_active_revision()
    snapshot = active.get("snapshot_data") or {}
    routes = snapshot.get("routes") if isinstance(snapshot, dict) else {}
    if not isinstance(routes, dict):
        routes = {}
    out: list[dict[str, Any]] = []
    for rid, cfg in routes.items():
        if route_id is not None and rid != route_id:
            continue
        cands = cfg.get("candidates") if isinstance(cfg, dict) else None
        if not isinstance(cands, list):
            continue
        for cand in cands:
            if not isinstance(cand, dict):
                continue
            out.append(
                {
                    "route_id": rid,
                    "upstream": cand.get("upstream"),
                    "model": cand.get("model"),
                    "weight": cand.get("weight", 1),
                }
            )
        # include fallback tier if present
        for cand in (cfg.get("fallback") if isinstance(cfg, dict) else None) or []:
            if not isinstance(cand, dict):
                continue
            out.append(
                {
                    "route_id": rid,
                    "upstream": cand.get("upstream"),
                    "model": cand.get("model"),
                    "weight": cand.get("weight", 1),
                    "tier": "fallback",
                }
            )
    return {"items": out, "total": len(out)}


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    """Control Plane settings + security status — never returns secret values."""
    return {
        "settings": dict(_settings),
        "security": {
            "encryption_key_configured": bool(os.getenv("SMART_ROUTER_ENCRYPTION_KEY")),
            "database_configured": bool(os.getenv("DATABASE_URL")),
            "redis_configured": bool(os.getenv("REDIS_URL")),
        },
    }


@router.put("/settings")
def update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    allowed = {"log_level"}
    updated_keys: list[str] = []
    for key in allowed:
        if key in payload:
            _settings[key] = str(payload[key])
            updated_keys.append(key)
            _audit_events.append(
                {"action": "settings.updated", "key": key, "created_at": datetime.now(UTC).isoformat()}
            )
    if not updated_keys:
        raise HTTPException(status_code=400, detail="no updatable settings provided")
    return {"settings": dict(_settings), "updated": updated_keys}


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


def _snapshot_to_dict(snapshot) -> dict[str, object]:
    """Convert RuntimeConfigSnapshot to JSON-serializable dict for revision storage."""
    conns: dict[str, object] = {}
    for cid, cfg in (getattr(snapshot, "connections", {}) or {}).items():
        conns[cid] = {
            "connection_id": getattr(cfg, "connection_id", cid),
            "base_url": getattr(cfg, "base_url", ""),
            "auth_mode": getattr(cfg, "auth_mode", "bearer"),
            "token_env": getattr(cfg, "token_env", ""),
        }
    routes: dict[str, object] = {}
    for rname, rcfg in (getattr(snapshot, "routes", {}) or {}).items():
        cand_list = []
        for c in getattr(rcfg, "candidates", []) or []:
            ref = getattr(c, "resource_ref", None)
            upstream = getattr(ref, "provider_connection_id", "") if ref else ""
            model = getattr(ref, "model_id", "") if ref else ""
            entry: dict[str, object] = {"upstream": upstream, "model": model, "weight": getattr(c, "weight", 1)}
            meta = getattr(c, "metadata", {}) or {}
            # merge relevant metadata without leaking secrets
            for k, v in meta.items():
                if k not in entry:
                    entry[k] = v
            cand_list.append(entry)
        fb_list = []
        for c in getattr(rcfg, "fallback", []) or []:
            ref = getattr(c, "resource_ref", None)
            upstream = getattr(ref, "provider_connection_id", "") if ref else ""
            model = getattr(ref, "model_id", "") if ref else ""
            entry = {"upstream": upstream, "model": model, "weight": getattr(c, "weight", 1)}
            meta = getattr(c, "metadata", {}) or {}
            for k, v in meta.items():
                if k not in entry:
                    entry[k] = v
            fb_list.append(entry)
        routes[rname] = {
            "strategy": getattr(rcfg, "strategy", "priority"),
            "candidates": cand_list,
            "fallback": fb_list,
            "generated": bool(getattr(rcfg, "generated", False)),
        }
    return {"connections": conns, "routes": routes}


@router.post("/migration/yaml", status_code=201)
async def migrate_legacy_yaml(request: Request) -> dict[str, object]:
    """Compile legacy YAML (AC-16) into an activated revision — Control Plane."""
    import yaml as _yaml
    from apps.gateway.config.compiler import LegacyConfigCompiler

    import json as _json
    from urllib.parse import parse_qs

    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    text = raw.decode("utf-8", errors="replace") if raw else ""
    yaml_data: str | None = None

    if "application/json" in content_type:
        try:
            payload = _json.loads(text)
            if isinstance(payload, dict):
                yaml_data = payload.get("yaml_data") or payload.get("yaml") or payload.get("content")
        except Exception:
            yaml_data = None
    elif "application/x-www-form-urlencoded" in content_type:
        parsed = parse_qs(text, keep_blank_values=True)
        for key in ("yaml_data", "yaml", "content"):
            if parsed.get(key):
                yaml_data = parsed[key][0]
                break
    else:
        # Treat the raw body as YAML text directly (content-type absent/other).
        yaml_data = text

    if not yaml_data or not str(yaml_data).strip():
        raise HTTPException(status_code=400, detail="yaml_data is required")
    yaml_str = str(yaml_data)
    try:
        cfg = _yaml.safe_load(yaml_str)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid YAML: {exc}") from exc
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="YAML must be a mapping")
    # basic validation: if routes present it must be a mapping
    routes_val = cfg.get("routes")
    if routes_val is not None and not isinstance(routes_val, dict):
        raise HTTPException(status_code=400, detail="routes must be a mapping")
    upstreams_val = cfg.get("upstreams")
    if upstreams_val is not None and not isinstance(upstreams_val, dict):
        raise HTTPException(status_code=400, detail="upstreams must be a mapping")
    try:
        snapshot = LegacyConfigCompiler().compile_dict(cfg)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    snapshot_data = _snapshot_to_dict(snapshot)
    revision_id = _revision_manager.create_draft(snapshot_data)
    valid, errors = _revision_manager.validate(revision_id)
    if not valid:
        raise HTTPException(status_code=400, detail=errors)
    _revision_manager.activate(revision_id)
    _audit_events.append({"action": "migration.yaml", "revision_id": revision_id, "created_at": datetime.now(UTC).isoformat()})
    return {"revision_id": revision_id, "activated": True, "snapshot_data": snapshot_data}


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
