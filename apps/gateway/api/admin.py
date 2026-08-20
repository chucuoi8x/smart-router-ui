"""Administrative control-plane API for provider setup and revisions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException

from apps.gateway.config.revision import ConfigRevisionManager
from apps.gateway.config.templates import ProviderTemplateRegistry


ADMIN_AUTH_TOKEN = "Bearer test-admin-key"


def require_admin_auth(authorization: str = Header(default="")) -> None:
    if authorization != ADMIN_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin credential")


router = APIRouter(dependencies=[Depends(require_admin_auth)])

_template_registry = ProviderTemplateRegistry()
_revision_manager = ConfigRevisionManager()
_provider_connections: dict[str, dict[str, Any]] = {}


def _serialize_revision(revision: dict[str, Any]) -> dict[str, Any]:
    result = dict(revision)
    created_at = result.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at.isoformat()
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
