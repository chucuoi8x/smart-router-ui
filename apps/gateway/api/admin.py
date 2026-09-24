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
from apps.gateway.db.dependencies import get_session, get_provider_registry_repo
from apps.gateway.db.revisions import RevisionRepository
from apps.gateway.quota.reservations import QuotaResource, QuotaResourceRepository
from apps.gateway.db.models import AttemptLedger, ProviderConnection, ProviderCredential, ProviderModel, RequestLedger, UsageLedger
from apps.gateway.db.provider_registry import ProviderRegistryRepository
from apps.gateway.db.control_plane import ControlPlaneRepository
from apps.gateway.providers.registry import default_driver_registry
from apps.gateway.providers.base import DriverNotFoundError, ProviderDiscoveryError
from apps.gateway.security.crypto import decrypt_secret, encrypt_secret
from apps.gateway.security.ssrf import validate_provider_url, ProviderURLValidationError
from apps.gateway.security.redaction import redact_secrets


ADMIN_AUTH_TOKEN_FALLBACK = "Bearer test-admin-key"


def _admin_expected_authorization() -> str:
    """Return the expected admin Authorization header value.

    In real/test deployments the gateway sets SMART_ROUTER_KEY. In unit
    tests the default is still the legacy constant so no test change is
    required. Prefer explicit env when available to align Control Plane
    auth with the gateway lifecycle.
    """
    key = os.getenv("SMART_ROUTER_KEY") or os.getenv("SMART_ROUTER_ADMIN_KEY")
    if key and key.strip():
        bearer = key.strip()
        if not bearer.lower().startswith("bearer "):
            bearer = f"Bearer {bearer}"
        return bearer
    return ADMIN_AUTH_TOKEN_FALLBACK


def require_admin_auth(authorization: str = Header(default="")) -> None:
    expected = _admin_expected_authorization()
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid admin credential")


router = APIRouter(dependencies=[Depends(require_admin_auth)])

_template_registry = ProviderTemplateRegistry()
# P0-05: ConfigRevisionManager retained only as a pure snapshot validator; all
# revision state is persisted through RevisionRepository(db).
_revision_validator = ConfigRevisionManager()
# PR-05: provider connections, credentials and imported models are persisted in
# the database through ProviderRegistryRepository; no in-memory registry state.
_provider_health_cache: dict[str, dict[str, Any]] = {}
_driver_registry = default_driver_registry()


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _serialize_project(project: Any) -> dict[str, Any]:
    return {
        "project_id": project.id,
        "name": project.name,
        "description": project.description,
        "active": project.is_active,
        "created_at": _serialize_datetime(project.created_at),
    }


def _serialize_key(key: Any, *, secret: str | None = None) -> dict[str, Any]:
    result = {
        "key_id": key.id,
        "project_id": key.project_id,
        "alias": key.alias,
        "active": key.is_active,
        "created_at": _serialize_datetime(key.created_at),
        "revoked_at": _serialize_datetime(key.revoked_at),
    }
    if secret is not None:
        result["secret"] = secret
    return result


def _serialize_budget(budget: Any) -> dict[str, Any]:
    return {
        "project_id": budget.project_id,
        "currency": budget.currency,
        "ceiling": budget.ceiling,
        "used": budget.used,
        "remaining": budget.ceiling - budget.used,
        "allow_paid_fallback": budget.allow_paid_fallback,
        "updated_at": _serialize_datetime(budget.updated_at),
    }


def _serialize_alert(alert: Any) -> dict[str, Any]:
    return {
        "alert_id": alert.id,
        "severity": alert.severity,
        "message": alert.message,
        "source": alert.source,
        "status": alert.status,
        "created_at": _serialize_datetime(alert.created_at),
    }


def _serialize_audit_event(event: Any) -> dict[str, Any]:
    return {
        "action": event.action,
        **dict(event.metadata_ or {}),
        "created_at": _serialize_datetime(event.created_at),
    }


def _redact_audit_event(event: dict[str, Any]) -> dict[str, Any]:
    safe = {k: v for k, v in event.items() if k.lower() not in {"api_key", "credential_encrypted", "credential", "secret", "token"}}
    for k, v in list(safe.items()):
        if isinstance(v, str) and v.startswith("sk-"):
            safe[k] = "[REDACTED]"
    return safe


async def _write_audit(db: AsyncSession, action: str, **metadata: Any) -> None:
    safe = {k: v for k, v in metadata.items() if k.lower() not in {"api_key", "secret", "token", "credential", "credential_encrypted"}}
    await ControlPlaneRepository(db).add_audit_event(action, safe)


def _control_repo(db: AsyncSession) -> ControlPlaneRepository:
    return ControlPlaneRepository(db)


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

def _serialize_credential(cred: ProviderCredential) -> dict[str, Any]:
    return {
        "credential_id": cred.id,
        "alias": cred.alias,
        "connection_id": cred.connection_id,
        "credential_present": True,
    }


def _serialize_revision(revision: Any) -> dict[str, Any]:
    """Serialize a persisted ConfigRevision ORM row into the API shape."""
    return {
        "revision_id": revision.id,
        "snapshot_data": revision.snapshot_data,
        "active": revision.is_active,
        "created_at": _serialize_datetime(revision.created_at),
        "activated_at": _serialize_datetime(revision.activated_at),
    }


async def _ensure_active_revision(db: AsyncSession) -> Any:
    """Return the DB active revision, seeding a default one when missing."""
    repo = RevisionRepository(db)
    active = await repo.get_active()
    if active is None:
        draft = await repo.create_draft({"routes": {}, "connections": {}})
        active = await repo.activate(draft.id)
        await db.commit()
    if active is None:
        raise HTTPException(status_code=500, detail="active revision unavailable")
    return active


def _serialize_provider(
    conn: ProviderConnection,
    credential_present: bool = False,
) -> dict[str, Any]:
    """Return provider metadata without credential ciphertext or plaintext."""
    return {
        "connection_id": conn.id,
        "template_id": conn.template_id,
        "name": conn.name,
        "base_url": conn.base_url,
        "driver": conn.driver,
        "active": conn.is_active,
        "disabled": not conn.is_active,
        "credential_present": credential_present,
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
async def list_providers(repo: ProviderRegistryRepository = Depends(get_provider_registry_repo)) -> dict[str, object]:
    connections = await repo.list_connections()
    cred_counts = await repo.credential_counts_by_connection()
    items = [_serialize_provider(c, credential_present=cred_counts.get(c.id, 0) > 0) for c in connections]
    return {"items": items, "total": len(items)}


@router.delete("/providers/{connection_id}")
async def delete_provider(
    connection_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repo = ProviderRegistryRepository(db)
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    name = conn.name
    await repo.delete_connection(connection_id)
    await _write_audit(db, "provider.deleted", connection_id=connection_id, name=name)
    await db.commit()
    return {"connection_id": connection_id, "deleted": True}


@router.get("/providers/health")
async def list_providers_health(
    repo: ProviderRegistryRepository = Depends(get_provider_registry_repo),
) -> dict[str, Any]:
    connections = await repo.list_connections()
    cred_counts = await repo.credential_counts_by_connection()
    now = datetime.now(UTC).isoformat()
    items = []
    for conn in connections:
        cached = _provider_health_cache.get(conn.id, {})
        items.append({
            "connection_id": conn.id,
            "template_id": conn.template_id,
            "name": conn.name,
            "base_url": conn.base_url,
            "driver": conn.driver,
            "status": cached.get("status", "unknown"),
            "checked_at": cached.get("checked_at", now),
            "runtime_state": cached.get("runtime_state"),
            "credential_present": cred_counts.get(conn.id, 0) > 0,
        })
    return {"items": items, "total": len(items)}


@router.get("/providers/{connection_id}/health")
async def get_provider_health(
    connection_id: str,
    repo: ProviderRegistryRepository = Depends(get_provider_registry_repo),
) -> dict[str, Any]:
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    cached = _provider_health_cache.get(connection_id, {})
    return {
        "connection_id": conn.id,
        "template_id": conn.template_id,
        "name": conn.name,
        "base_url": conn.base_url,
        "driver": conn.driver,
        "status": cached.get("status", "unknown"),
        "checked_at": cached.get("checked_at", datetime.now(UTC).isoformat()),
        "runtime_state": cached.get("runtime_state"),
        "credential_present": await repo.has_credential(connection_id),
    }


@router.put("/providers/{connection_id}")
async def update_provider(
    connection_id: str,
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    repo = ProviderRegistryRepository(db)
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    fields: dict[str, Any] = {}
    name = payload.get("name")
    if name is not None:
        cleaned = str(name).strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        fields["name"] = cleaned
    base_url = payload.get("base_url")
    if base_url is not None:
        cleaned_url = str(base_url).strip()
        if not cleaned_url:
            raise HTTPException(status_code=400, detail="base_url cannot be empty")
        allow_private = bool(payload.get("allow_private_network"))
        try:
            validate_provider_url(cleaned_url, allow_private_network=allow_private)
        except ProviderURLValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        fields["base_url"] = cleaned_url
    driver = payload.get("driver")
    if driver is not None:
        fields["driver"] = str(driver)
    if "active" in payload:
        fields["is_active"] = bool(payload["active"])
    api_key = payload.get("api_key")
    if api_key:
        encrypted = encrypt_secret(str(api_key))
        # New credential rows are the source of truth; the legacy column is kept
        # in sync so pre-PR-05 readers still resolve a credential.
        fields["credential_encrypted"] = encrypted
    if fields:
        await repo.update_connection(conn, **fields)
        if api_key:
            await repo.create_credential(
                connection_id=connection_id,
                alias="updated",
                credential_encrypted=fields["credential_encrypted"],
            )
    timestamp = datetime.now(UTC).isoformat()
    await _write_audit(db, "provider.updated", connection_id=connection_id, fields=sorted([k for k in payload if k != "api_key"]) + (["credential_present"] if api_key else []))
    await db.commit()
    return _serialize_provider(conn, credential_present=await repo.has_credential(connection_id))


@router.post("/providers/{connection_id}/deactivate")
async def deactivate_provider(
    connection_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    repo = ProviderRegistryRepository(db)
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    await repo.set_connection_active(conn, False)
    timestamp = datetime.now(UTC).isoformat()
    await _write_audit(db, "provider.deactivated", connection_id=connection_id)
    await db.commit()
    return {"connection_id": connection_id, "active": False, "disabled": True, "changed_at": timestamp}


@router.post("/providers/{connection_id}/reactivate")
async def reactivate_provider(
    connection_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    repo = ProviderRegistryRepository(db)
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    await repo.set_connection_active(conn, True)
    timestamp = datetime.now(UTC).isoformat()
    await _write_audit(db, "provider.reactivated", connection_id=connection_id)
    await db.commit()
    return {"connection_id": connection_id, "active": True, "disabled": False, "changed_at": timestamp}


@router.get("/providers/{connection_id}")
async def get_provider(connection_id: str, repo: ProviderRegistryRepository = Depends(get_provider_registry_repo)) -> dict[str, object]:
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    has_cred = await repo.has_credential(connection_id)
    return _serialize_provider(conn, credential_present=has_cred)


@router.post("/providers", status_code=201)
async def create_provider(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    repo = ProviderRegistryRepository(db)
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
    try:
        validate_provider_url(base_url, allow_private_network=bool(payload.get("allow_private_network")))
    except ProviderURLValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    driver = str(payload.get("driver") or template.get("driver") or "")
    encrypted = encrypt_secret(str(payload["api_key"])) if payload.get("api_key") else None
    # The api_key supplied at creation time becomes the connection's own
    # credential (legacy single-column path).  Additional credentials are added
    # explicitly via /credentials and listed separately, so create must not
    # duplicate it into a ProviderCredential row.
    conn = await repo.create_connection(
        name=name,
        template_id=template_id,
        driver=driver,
        base_url=base_url,
        credential_encrypted=encrypted,
    )
    await _ensure_active_revision(db)
    await _write_audit(db, "provider.created", connection_id=conn.id, name=name)
    await db.commit()
    return _serialize_provider(conn, credential_present=bool(encrypted))


@router.post("/providers/{connection_id}/test")
async def test_provider_connection(
    connection_id: str,
    repo: ProviderRegistryRepository = Depends(get_provider_registry_repo)
) -> dict[str, Any]:
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    driver_id = str(conn.driver or conn.template_id or "generic-openai")
    try:
        driver_cls = _driver_registry.resolve(driver_id)
    except DriverNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        cred_text = await repo.resolve_credential(conn)
        ctx = {
            "connection_id": conn.id,
            "base_url": conn.base_url,
            "driver": driver_id,
            "template_id": conn.template_id,
            "credential": {"api_key": decrypt_secret(cred_text)} if cred_text else {},
        }
        # Single factory for every driver instance (plan P0-03 "Driver factory").
        driver = _driver_registry.create(driver_id, ctx)
        result = await driver.validate_connection(ctx)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    normalized_result = result if isinstance(result, dict) else {"result": result}
    result_status = str(normalized_result.get("status", "ok")).lower()
    health_status = "healthy" if result_status in {"ok", "healthy", "success"} else "degraded"
    try:
        from apps.gateway.quota.runtime import describe_runtime_state as _describe_runtime_state
        _probe_kind = str(normalized_result.get("kind") or normalized_result.get("error_kind") or ("SUCCESS" if health_status == "healthy" else "UNKNOWN")).upper()
        _runtime_state = _describe_runtime_state(
            _probe_kind,
            reset_at=str(normalized_result.get("reset_at")) if normalized_result.get("reset_at") else None,
            retry_after=str(normalized_result.get("retry_after")) if normalized_result.get("retry_after") else None,
        )
        if health_status == "healthy" and _probe_kind in {"SUCCESS", "UNKNOWN"}:
            _runtime_state = {"state": "healthy", "kind": "SUCCESS", "retryable": False, "is_long_term": False, "reset_at": None, "retry_after": None, "scope": None}
    except Exception:
        _runtime_state = None
    _provider_health_cache[connection_id] = {
        "status": health_status,
        "checked_at": datetime.now(UTC).isoformat(),
        "runtime_state": _runtime_state,
        "result": {k: v for k, v in normalized_result.items() if k not in {"api_key", "credential", "token", "secret"}},
    }
    if health_status != "healthy":
        from fastapi.responses import JSONResponse
        kind = str(normalized_result.get("kind") or normalized_result.get("error_kind") or "INVALID_RESPONSE").upper()
        status = {"AUTH_EXPIRED": 401, "AUTH_FAILED": 401, "AUTH_REVOKED": 403,
                  "CONFIG_INVALID": 400, "TIMEOUT": 504}.get(kind, 502)
        return JSONResponse(status_code=status, content={"ok": False, "connection_id": connection_id,
            "error_kind": kind, "message": normalized_result.get("detail") or "Provider validation failed"})
    return {"ok": True, "connection_id": connection_id, "result": normalized_result}


@router.post("/providers/{connection_id}/discover")
async def discover_provider_models(
    connection_id: str,
    repo: ProviderRegistryRepository = Depends(get_provider_registry_repo),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    driver_id = conn.driver or conn.template_id or "generic-openai"
    try:
        driver_cls = _driver_registry.resolve(driver_id)
    except DriverNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        cred_text = await repo.resolve_credential(conn)
        ctx = {
            "connection_id": conn.id,
            "base_url": conn.base_url,
            "driver": driver_id,
            "template_id": conn.template_id,
            "credential": {"api_key": decrypt_secret(cred_text)} if cred_text else {},
        }
        # Single factory for every driver instance (plan P0-03 "Driver factory").
        driver = _driver_registry.create(driver_id, ctx)
        models = await driver.discover_models(ctx)  # type: ignore[func-returns-value]
    except ProviderDiscoveryError as exc:
        status = exc.status_code or {"AUTH_EXPIRED": 401, "AUTH_FAILED": 401,
                                     "AUTH_REVOKED": 403, "CONFIG_INVALID": 400,
                                     "TIMEOUT": 504}.get(exc.kind, 502)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=status, content={
            "ok": False, "connection_id": connection_id,
            "error_kind": exc.kind, "message": exc.detail,
        })
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

    # Persist discovered models
    for model_entry in normalized:
        model_id = model_entry.get("id")
        if model_id:
            await repo.import_model(
                connection_id=conn.id,
                model_id=model_id,
                metadata_=model_entry,
            )

    return {"connection_id": connection_id, "models": normalized, "count": len(normalized)}


@router.post("/providers/{connection_id}/models/import", status_code=201)
async def import_provider_models(
    connection_id: str,
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    repo = ProviderRegistryRepository(db)
    conn = await repo.get_connection(connection_id)
    if conn is None:
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
    active = await _ensure_active_revision(db)
    snapshot = copy.deepcopy(active.snapshot_data or {})
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
    rev_repo = RevisionRepository(db)
    draft = await rev_repo.create_draft(snapshot)
    errs = _revision_validator._validate_snapshot(snapshot)
    if errs:
        raise HTTPException(status_code=400, detail=errs)
    await rev_repo.activate(draft.id)
    rev_id = draft.id
    for mdl in added:
        await repo.import_model(connection_id=conn.id, model_id=mdl)
    await _write_audit(db, "route.models.imported", route_id=route_id, provider_id=connection_id, imported=added, revision_id=rev_id)
    await db.commit()
    return {"route_id":route_id,"revision_id":rev_id,"imported":added,"candidate_count":len(candidates)}


@router.post("/providers/{connection_id}/credentials", status_code=201)
async def add_provider_credential(
    connection_id: str,
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    repo = ProviderRegistryRepository(db)
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    alias = str(payload.get("alias") or payload.get("name") or f"cred_{uuid.uuid4().hex[:8]}").strip()
    cred = await repo.create_credential(
        connection_id=connection_id,
        alias=alias,
        credential_encrypted=encrypt_secret(api_key),
    )
    await _write_audit(db, "credential.added", connection_id=connection_id, credential_id=cred.id, alias=alias)
    await db.commit()
    return _serialize_credential(cred)


@router.get("/providers/{connection_id}/credentials")
async def list_provider_credentials(
    connection_id: str,
    repo: ProviderRegistryRepository = Depends(get_provider_registry_repo),
) -> dict[str, Any]:
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    creds = await repo.list_credentials(connection_id)
    items = [_serialize_credential(c) for c in creds]
    return {"items": items, "total": len(items), "connection_id": connection_id}


@router.delete("/providers/{connection_id}/credentials/{credential_id}")
async def delete_provider_credential(
    connection_id: str,
    credential_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    repo = ProviderRegistryRepository(db)
    conn = await repo.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="provider not found")
    cred = await repo.get_credential(credential_id)
    if cred is None or cred.connection_id != connection_id:
        raise HTTPException(status_code=404, detail="credential not found")
    alias = cred.alias
    await repo.delete_credential(credential_id)
    await _write_audit(db, "credential.deleted", connection_id=connection_id, credential_id=credential_id, alias=alias)
    await db.commit()
    return {"deleted": True, "credential_id": credential_id}


@router.post("/projects", status_code=201)
async def create_project(payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    secret_key = f"sr_{uuid.uuid4().hex}"
    repo = _control_repo(db)
    project = await repo.create_project(name=name, description=str(payload.get("description") or ""), secret_key_hash=encrypt_secret(secret_key))
    await _write_audit(db, "project.created", project_id=project.id, name=name)
    await db.commit()
    return {"project_id": project.id, "name": name, "description": project.description, "secret_key": secret_key}


@router.get("/projects")
async def list_projects(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    items = [_serialize_project(p) for p in await _control_repo(db).list_projects()]
    return {"items": items, "total": len(items)}


@router.post("/projects/{project_id}/keys", status_code=201)
async def create_project_key(project_id: str, payload: dict[str, Any] = Body(default={}), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    if await repo.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    alias = str(payload.get("alias") or "default").strip()
    if not alias:
        raise HTTPException(status_code=400, detail="alias cannot be empty")
    secret = f"srk_{uuid.uuid4().hex}"
    key = await repo.create_project_key(project_id=project_id, alias=alias, secret_encrypted=encrypt_secret(secret))
    await _write_audit(db, "project.key.created", project_id=project_id, key_id=key.id, alias=alias)
    await db.commit()
    return _serialize_key(key, secret=secret)


@router.get("/projects/{project_id}/keys")
async def list_project_keys(project_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    if await repo.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    items = [_serialize_key(k) for k in await repo.list_project_keys(project_id)]
    return {"items": items, "total": len(items)}


@router.delete("/projects/{project_id}/keys/{key_id}", status_code=200)
async def revoke_project_key(project_id: str, key_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    if await repo.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    key = await repo.get_project_key(project_id, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail="project key not found")
    if key.is_active:
        await repo.revoke_project_key(key)
        await _write_audit(db, "project.key.revoked", project_id=project_id, key_id=key_id)
        await db.commit()
    return {"project_id": project_id, "key_id": key_id, "revoked": True, "active": False, "revoked_at": _serialize_datetime(key.revoked_at)}


@router.put("/projects/{project_id}/budget")
async def upsert_project_budget(project_id: str, payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    if await repo.get_project(project_id) is None:
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
    currency = str(payload.get("currency") or "USD").strip().upper() or "USD"
    allow_paid = bool(payload.get("allow_paid_fallback", True))
    budget = await repo.upsert_budget(project_id=project_id, currency=currency, ceiling=ceiling, used=used, allow_paid_fallback=allow_paid)
    await _write_audit(db, "project.budget.updated", project_id=project_id, ceiling=ceiling, used=used)
    await db.commit()
    return _serialize_budget(budget)


@router.get("/projects/{project_id}/budget")
async def get_project_budget(project_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    if await repo.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    budget = await repo.get_budget(project_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget not found")
    return _serialize_budget(budget)


@router.get("/policies/paid-fallback")
async def get_paid_fallback_policy(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await _control_repo(db).get_policy("paid-fallback")


@router.put("/policies/paid-fallback")
async def update_paid_fallback_policy(payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="enabled is required")
    enabled = bool(payload["enabled"])
    project_id = payload.get("project_id")
    repo = _control_repo(db)
    if enabled:
        if project_id is not None:
            project_id = str(project_id)
            if await repo.get_project(project_id) is None:
                raise HTTPException(status_code=404, detail="project not found")
            budget = await repo.get_budget(project_id)
            if budget is None or not budget.allow_paid_fallback:
                raise HTTPException(status_code=400, detail="paid fallback requires budget with allow_paid_fallback=true")
            if budget.ceiling > 0 and budget.ceiling - budget.used <= 0:
                raise HTTPException(status_code=400, detail="budget exhausted: cannot enable paid fallback")
        elif not any(b.allow_paid_fallback for b in await repo.list_budgets()):
            raise HTTPException(status_code=400, detail="paid fallback requires at least one budget with allow_paid_fallback=true")
    record = await repo.set_policy("paid-fallback", enabled=enabled, requires_budget=True, project_id=project_id)
    await _write_audit(db, "policy.paid_fallback.updated", enabled=enabled, project_id=project_id)
    await db.commit()
    return record


@router.get("/budgets")
async def list_budgets(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    items = [_serialize_budget(v) for v in await _control_repo(db).list_budgets()]
    return {"items": items, "total": len(items)}


@router.get("/projects/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    project = await _control_repo(db).get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _serialize_project(project)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    project = await repo.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    name = project.name
    await repo.delete_project(project_id)
    await _write_audit(db, "project.deleted", project_id=project_id, name=name)
    await db.commit()
    return {"project_id": project_id, "deleted": True}


@router.post("/alerts", status_code=201)
async def create_alert(payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    severity = str(payload.get("severity") or "info").strip().lower()
    if severity not in {"info", "warning", "critical"}:
        raise HTTPException(status_code=400, detail="severity must be info, warning or critical")
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    alert = await _control_repo(db).create_alert(severity=severity, message=message, source=str(payload.get("source") or "manual"))
    await _write_audit(db, "alert.created", alert_id=alert.id, severity=severity)
    await db.commit()
    return _serialize_alert(alert)


@router.get("/alerts")
async def list_alerts(status: str | None = Query(default=None), severity: str | None = Query(default=None), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    alerts = await _control_repo(db).list_alerts(status=status, severity=severity)
    items = [_serialize_alert(a) for a in alerts]
    return {"items": items, "total": len(items)}


@router.patch("/alerts/{alert_id}")
async def update_alert(alert_id: str, payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    alert = await repo.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    new_status = str(payload.get("status") or "").strip().lower()
    if new_status not in {"open", "acknowledged", "resolved"}:
        raise HTTPException(status_code=400, detail="status must be open, acknowledged or resolved")
    await repo.set_alert_status(alert, new_status)
    await _write_audit(db, "alert.updated", alert_id=alert_id, status=new_status)
    await db.commit()
    return _serialize_alert(alert)


def _serialize_quota_resource(resource: QuotaResource) -> dict[str, Any]:
    return {
        "resource_id": resource.resource_id,
        "scope": resource.scope,
        "metric": resource.metric,
        "limit": resource.limit,
        "used": resource.used,
        "window_seconds": resource.window_seconds,
        "safety_buffer": resource.safety_buffer,
        "hard_limit": resource.hard_limit,
        "source": resource.source,
        "confidence": resource.confidence,
        "shared_group_id": resource.shared_group_id,
        "parent_id": resource.parent_id,
        "reset_at": resource.reset_at.isoformat() if isinstance(resource.reset_at, datetime) else resource.reset_at,
        "window_metadata": dict(resource.window_metadata or {}),
        "remaining": resource.effective_remaining,
    }


def _parse_reset_at(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="reset_at must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="reset_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@router.post("/quota/resources", status_code=201)
async def create_quota_resource(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    resource_id = str(payload.get("resource_id") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    metric = str(payload.get("metric") or "").strip()
    if not resource_id or not scope or not metric:
        raise HTTPException(status_code=400, detail="resource_id, scope and metric are required")
    repo = QuotaResourceRepository(db)
    if await repo.get_resource(resource_id) is not None:
        raise HTTPException(status_code=409, detail="quota resource already exists")
    try:
        resource = QuotaResource(
            resource_id=resource_id,
            scope=scope,
            metric=metric,
            limit=int(payload.get("limit")),
            used=int(payload.get("used", 0)),
            window_seconds=int(payload.get("window_seconds")),
            safety_buffer=int(payload.get("safety_buffer", 0)),
            hard_limit=bool(payload.get("hard_limit", True)),
            source=str(payload.get("source") or "configured"),
            confidence=str(payload.get("confidence") or "high"),
            shared_group_id=payload.get("shared_group_id"),
            parent_id=payload.get("parent_id"),
            reset_at=_parse_reset_at(payload.get("reset_at")),
            window_metadata=dict(payload.get("window_metadata") or {}),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await repo.save_resource(resource, commit=True)
    await _write_audit(db, "quota.resource.created", resource_id=resource_id, metric=metric)
    await db.commit()
    return _serialize_quota_resource(resource)


@router.get("/quota/resources")
async def list_quota_resources(
    scope: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    items = await QuotaResourceRepository(db).list_resources()
    if scope:
        items = [r for r in items if r.scope == scope]
    if metric:
        items = [r for r in items if r.metric == metric]
    data = [_serialize_quota_resource(r) for r in items]
    return {"items": data, "total": len(data)}


@router.get("/quota/resources/{resource_id}")
async def get_quota_resource(
    resource_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    resource = await QuotaResourceRepository(db).get_resource(resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="quota resource not found")
    return _serialize_quota_resource(resource)


@router.get("/routes")
async def list_routes(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """List routes from active immutable configuration revision."""
    active = await _ensure_active_revision(db)
    snapshot = active.snapshot_data or {}
    routes = snapshot.get("routes") if isinstance(snapshot, dict) else {}
    routes = routes if isinstance(routes, dict) else {}
    items = [
        {"route_id": route_id, "config": copy.deepcopy(config)}
        for route_id, config in routes.items()
    ]
    return {"items": items, "total": len(items), "revision_id": active.id}


@router.put("/routes/{route_id}")
async def update_route(route_id: str, payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Create and activate a validated revision containing updated route policy."""
    allowed_strategies = {"priority", "weighted", "smart", "failover"}
    strategy = payload.get("strategy")
    if strategy is not None and str(strategy) not in allowed_strategies:
        raise HTTPException(status_code=400, detail="unsupported route strategy")

    active = await _ensure_active_revision(db)
    snapshot = copy.deepcopy(active.snapshot_data or {})
    routes = snapshot.get("routes")
    if not isinstance(routes, dict) or route_id not in routes:
        raise HTTPException(status_code=404, detail="route not found")

    updated = copy.deepcopy(payload)
    routes[route_id] = updated
    snapshot["routes"] = routes
    rev_repo = RevisionRepository(db)
    draft = await rev_repo.create_draft(snapshot)
    errors = _revision_validator._validate_snapshot(snapshot)
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    await rev_repo.activate(draft.id)
    revision_id = draft.id
    await _write_audit(db, "route.updated", route_id=route_id, revision_id=revision_id)
    await db.commit()
    return {
        "route_id": route_id,
        "config": updated,
        "revision_id": revision_id,
    }


@router.get("/revisions/active")
async def get_active_revision(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return _serialize_revision(await _ensure_active_revision(db))


@router.get("/revisions")
async def list_revisions(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    revisions = await RevisionRepository(db).list_revisions()
    items = [_serialize_revision(r) for r in revisions]
    return {"items": items, "total": len(items)}


@router.get("/revisions/{revision_id}")
async def get_revision_detail(revision_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rev = await RevisionRepository(db).get_revision(revision_id)
    if rev is None:
        raise HTTPException(status_code=404, detail="revision not found")
    return _serialize_revision(rev)


@router.post("/revisions", status_code=201)
async def create_revision(payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, str]:
    draft = await RevisionRepository(db).create_draft(payload)
    await db.commit()
    return {"revision_id": draft.id}


@router.post("/revisions/{revision_id}/rollback")
async def rollback_revision(request: Request, revision_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Activate a prior immutable revision with an explicit rollback audit event."""
    rev_repo = RevisionRepository(db)
    target = await rev_repo.get_revision(revision_id)
    if target is None:
        raise HTTPException(status_code=404, detail="revision not found")
    current = await rev_repo.get_active()
    if current is not None and current.id == revision_id:
        raise HTTPException(status_code=400, detail="revision is already active")
    previous_id = current.id if current else None
    errors = _revision_validator._validate_snapshot(target.snapshot_data)
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    manager = getattr(request.app.state, "config_manager", None)
    if manager is not None:
        # P0-20 authority: compile+validate first (raises → last-known-good
        # runtime and DB active pointer both untouched), then flip DB + audit +
        # runtime together in one durable transaction.
        try:
            snapshot = await manager.validate_revision(revision_id, db)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=[str(exc)]) from exc
        await rev_repo.activate(revision_id)
        await _write_audit(db, "revision.rolled_back", revision_id=revision_id, from_revision_id=previous_id)
        await db.commit()
        manager.set_active_runtime(snapshot, revision_id)
        await manager.publish_activation(revision_id)
    else:
        await rev_repo.activate(revision_id)
        await _write_audit(db, "revision.rolled_back", revision_id=revision_id, from_revision_id=previous_id)
        await db.commit()
    result = _serialize_revision(await _ensure_active_revision(db))
    result["rolled_back_from"] = previous_id
    return result


@router.post("/revisions/{revision_id}/activate")
async def activate_revision(request: Request, revision_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rev_repo = RevisionRepository(db)
    target = await rev_repo.get_revision(revision_id)
    if target is None:
        raise HTTPException(status_code=404, detail=["Revision not found"])
    errors = _revision_validator._validate_snapshot(target.snapshot_data)
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    manager = getattr(request.app.state, "config_manager", None)
    if manager is not None:
        try:
            snapshot = await manager.validate_revision(revision_id, db)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=[str(exc)]) from exc
        await rev_repo.activate(revision_id)
        await _write_audit(db, "revision.activated", revision_id=revision_id)
        await db.commit()
        manager.set_active_runtime(snapshot, revision_id)
        await manager.publish_activation(revision_id)
    else:
        await rev_repo.activate(revision_id)
        await _write_audit(db, "revision.activated", revision_id=revision_id)
        await db.commit()
    return _serialize_revision(await _ensure_active_revision(db))


@router.get("/audit/export")
async def export_audit(action: str | None = Query(default=None), since: str | None = Query(default=None), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    events = await _control_repo(db).list_audit_events(action=action, since=_parse_audit_since(since))
    items = [_redact_audit_event(_serialize_audit_event(e)) for e in events]
    return {"items": items, "total": len(items), "exported_at": datetime.now(UTC).isoformat()}


@router.get("/audit")
async def list_audit(action: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=500), offset: int = Query(default=0, ge=0), since: str | None = Query(default=None), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    since_dt = _parse_audit_since(since)
    filtered = await repo.list_audit_events(action=action, since=since_dt)
    total = len(filtered)
    events = filtered[offset:offset + limit]
    items = [_redact_audit_event(_serialize_audit_event(e)) for e in events]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/models")
async def list_models(route_id: str | None = Query(default=None), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """List model resources from active revision — Control Plane view (AC-13)."""
    active = await _ensure_active_revision(db)
    snapshot = active.snapshot_data or {}
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
async def get_settings(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return {
        "settings": await _control_repo(db).get_settings(),
        "security": {
            "encryption_key_configured": bool(os.getenv("SMART_ROUTER_ENCRYPTION_KEY")),
            "database_configured": bool(os.getenv("DATABASE_URL")),
            "redis_configured": bool(os.getenv("REDIS_URL")),
        },
    }


@router.put("/settings")
async def update_settings(payload: dict[str, Any] = Body(...), db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    repo = _control_repo(db)
    updated_keys = [key for key in {"log_level"} if key in payload]
    if not updated_keys:
        raise HTTPException(status_code=400, detail="no updatable settings provided")
    for key in updated_keys:
        await repo.set_setting(key, str(payload[key]))
        await _write_audit(db, "settings.updated", key=key)
    await db.commit()
    return {"settings": await repo.get_settings(), "updated": updated_keys}


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
    repo: ProviderRegistryRepository = Depends(get_provider_registry_repo),
) -> dict:
    """Aggregate Control Plane overview — no secrets exposed (AC-13)."""
    # 1. Provider connections (DB-backed)
    connections = await repo.list_connections()
    cred_counts = await repo.credential_counts_by_connection()
    providers = {
        "count": len(connections),
        "items": [
            {
                "connection_id": c.id,
                "name": c.name,
                "template_id": c.template_id,
                "credential_present": cred_counts.get(c.id, 0) > 0,
            }
            for c in connections
        ],
    }

    # 2. Active revision
    active = None
    try:
        rev = await _ensure_active_revision(db)
        active = {
            "revision_id": rev.id,
            "created_at": rev.created_at.isoformat() if rev.created_at else None,
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

    # 4. Policies and budgets (AC-10 visibility, no secrets)
    control_repo = _control_repo(db)
    budgets = await control_repo.list_budgets()
    budgets_snapshot = {"items": [_serialize_budget(v) for v in budgets], "total": len(budgets)}
    policies_snapshot = {"paid_fallback": await control_repo.get_policy("paid-fallback")}

    return {
        "providers": providers,
        "active_revision": active,
        "revisions": {"active": active},
        "usage": stats,
        "policies": policies_snapshot,
        "budgets": budgets_snapshot,
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
async def migrate_legacy_yaml(request: Request, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
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
    rev_repo = RevisionRepository(db)
    draft = await rev_repo.create_draft(snapshot_data)
    errors = _revision_validator._validate_snapshot(snapshot_data)
    if errors:
        raise HTTPException(status_code=400, detail=errors)
    await rev_repo.activate(draft.id)
    revision_id = draft.id
    await _write_audit(db, "migration.yaml", revision_id=revision_id)
    await db.commit()
    return {"revision_id": revision_id, "activated": True, "snapshot_data": snapshot_data}


@router.post("/routes/simulate")
async def simulate_route_endpoint(
    request: Request,
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_session),
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

    # AC-10: optional project budget gate for simulation visibility
    project_id = payload.get("project_id") or payload.get("project") or payload.get("projectId")
    budget_context: dict[str, object] | None = None
    control_repo = _control_repo(db)
    configured_policy = await control_repo.get_policy("paid-fallback")
    configured_policy_project = configured_policy.get("project_id")
    policy_enabled = bool(configured_policy.get("enabled")) and (
        configured_policy_project is None or configured_policy_project == project_id
    )
    policy_context: dict[str, object] = {
        "paid_fallback_enabled": policy_enabled,
        "requires_budget": bool(configured_policy.get("requires_budget", True)),
        "project_id": project_id if policy_enabled else None,
    }
    if isinstance(project_id, str) and project_id.strip():
        project_id = project_id.strip()
        if await control_repo.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        budget = await control_repo.get_budget(project_id)
        if budget is not None:
            remaining = float(budget.ceiling - budget.used)
            eligible = remaining > 0
            # normalize remaining to float for consistent API
            budget_context = {
                "project_id": project_id,
                "currency": budget.currency,
                "ceiling": float(budget.ceiling),
                "used": float(budget.used),
                "remaining": float(remaining),
                "eligible": bool(eligible),
                "allow_paid_fallback": bool(budget.allow_paid_fallback),
            }
            if not eligible:
                return {
                    "route_name": route_name,
                    "preset": None,
                    "candidates": [],
                    "selected_resource": None,
                    "reason": "project_budget_exhausted",
                    "expected_reservation": None,
                    "budget": budget_context,
                    "policy": policy_context,
                }
        else:
            # Project exists but no explicit budget → treat as eligible with no ceiling
            budget_context = {
                "project_id": project_id,
                "currency": "USD",
                "ceiling": None,
                "used": 0.0,
                "remaining": None,
                "eligible": True,
                "allow_paid_fallback": True,
            }
    else:
        project_id = None

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

    response: dict[str, object] = {
        "route_name": result.route_name,
        "preset": result.preset,
        "candidates": result.candidates,
        "selected_resource": result.selected_resource,
        "reason": result.reason,
        "expected_reservation": result.expected_reservation,
    }
    if budget_context is not None:
        response["budget"] = budget_context
    response["policy"] = policy_context
    return response
