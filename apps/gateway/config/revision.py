"""Config revision lifecycle with validation and last-known-good activation."""
from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Tuple

from apps.gateway.config.snapshot import RuntimeConfigSnapshot


_ALLOWED_STRATEGIES = {"priority", "weighted", "smart", "failover"}


class ConfigRevisionManager:
    """Own immutable revision drafts and atomically swap active revision."""

    def __init__(self):
        self._revisions: Dict[str, Dict[str, Any]] = {}
        self._active_revision_id: Optional[str] = None

    def create_draft(self, snapshot_data: Dict[str, Any]) -> str:
        if not isinstance(snapshot_data, dict):
            raise TypeError("snapshot_data must be a mapping")
        revision_id = f"rev_{uuid.uuid4().hex[:12]}"
        snapshot = copy.deepcopy(snapshot_data)
        snapshot.setdefault("routes", {})
        snapshot.setdefault("connections", {})
        self._revisions[revision_id] = {
            "revision_id": revision_id,
            "snapshot_data": snapshot,
            "active": False,
            "created_at": datetime.now(UTC),
        }
        return revision_id

    def validate(self, revision_id: str) -> Tuple[bool, List[str]]:
        revision = self._revisions.get(revision_id)
        if revision is None:
            return False, ["Revision not found"]
        snapshot = revision.get("snapshot_data")
        errors = self._validate_snapshot(snapshot)
        revision["validation_errors"] = errors
        revision["validated_at"] = datetime.now(UTC)
        return not errors, errors

    @staticmethod
    def _validate_snapshot(snapshot: Any) -> List[str]:
        errors: List[str] = []
        if not isinstance(snapshot, dict):
            return ["snapshot_data must be a mapping"]
        routes = snapshot.get("routes", {})
        connections = snapshot.get("connections", {})
        if not isinstance(routes, dict):
            errors.append("routes must be a mapping")
            return errors
        if not isinstance(connections, dict):
            errors.append("connections must be a mapping")
            connections = {}

        for route_id, route in routes.items():
            if not isinstance(route_id, str) or not route_id.strip():
                errors.append("route id must be a non-empty string")
                continue
            if not isinstance(route, dict):
                errors.append(f"route {route_id}: config must be a mapping")
                continue
            strategy = route.get("strategy", "priority")
            if strategy not in _ALLOWED_STRATEGIES:
                errors.append(f"route {route_id}: unsupported strategy {strategy!r}")
            for field in ("candidates", "fallback"):
                items = route.get(field, [])
                if not isinstance(items, list):
                    errors.append(f"route {route_id}: {field} must be a list")
                    continue
                for index, candidate in enumerate(items):
                    prefix = f"route {route_id} {field}[{index}]"
                    if not isinstance(candidate, dict):
                        errors.append(f"{prefix}: candidate must be a mapping")
                        continue
                    upstream = candidate.get("upstream")
                    model = candidate.get("model")
                    if not isinstance(upstream, str) or not upstream.strip():
                        errors.append(f"{prefix}: upstream is required")
                    elif connections and upstream not in connections:
                        errors.append(f"{prefix}: upstream {upstream!r} not found")
                    if not isinstance(model, str) or not model.strip():
                        errors.append(f"{prefix}: model is required")
                    weight = candidate.get("weight", 1)
                    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
                        errors.append(f"{prefix}: weight must be greater than zero")
        return errors

    def activate(self, revision_id: str) -> None:
        revision = self._revisions.get(revision_id)
        if revision is None:
            raise KeyError("Revision not found")
        valid, errors = self.validate(revision_id)
        if not valid:
            raise ValueError(errors)
        old_id = self._active_revision_id
        # Validate completed before mutating active state: last-known-good survives.
        if old_id and old_id in self._revisions:
            self._revisions[old_id]["active"] = False
        revision["active"] = True
        revision["activated_at"] = datetime.now(UTC)
        self._active_revision_id = revision_id

    def list_revisions(self) -> list[Dict[str, Any]]:
        return sorted(
            self._revisions.values(),
            key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    def get_active_revision(self) -> Optional[Dict[str, Any]]:
        if self._active_revision_id:
            return self._revisions.get(self._active_revision_id)
        return None

    def get_active_snapshot(self) -> Optional[RuntimeConfigSnapshot]:
        rev = self.get_active_revision()
        if not rev:
            return None
        return RuntimeConfigSnapshot()
