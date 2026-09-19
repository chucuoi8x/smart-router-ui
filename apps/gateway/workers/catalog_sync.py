"""Provider-agnostic catalog sync worker.

Moves all catalog synchronization logic out of the router core so that
router.py contains zero provider-specific branching. Reads provider name
and config keys dynamically from the configuration, with backward
compatibility for legacy ``aibox_catalog_sync`` and ``aibox_auto_promotion``
keys.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from apps.worker.collectors.aibox_catalog import (
    build_records,
    select_routes,
    state_from_records,
)

# Re-export for backward compatibility (router.py re-exports these)
__all__ = [
    "CatalogSyncError",
    "CatalogSyncWorker",
    "build_records",
    "select_routes",
    "state_from_records",
]


class CatalogSyncError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    import uuid
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_yaml(path: Path, value: Any) -> None:
    import uuid
    import yaml
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(value, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class CatalogSyncWorker:
    """Encapsulates catalog sync logic that was previously in SmartRouter.

    Reads provider name and config keys dynamically. Backward compatible
    with legacy ``aibox_catalog_sync`` and ``aibox_auto_promotion`` keys.
    """

    def __init__(
        self,
        config: dict[str, Any],
        clients: dict[str, httpx.AsyncClient],
        base_dir: Path,
        logger: Any,
        timeout_fn: Any,
        upstream_config_fn: Any,
        upstream_headers_fn: Any,
    ) -> None:
        self.config = config
        self.clients = clients
        self.base_dir = base_dir
        self.logger = logger
        self._timeout_fn = timeout_fn
        self._upstream_config_fn = upstream_config_fn
        self._upstream_headers_fn = upstream_headers_fn
        self.sync_running = False
        self.catalog: dict[str, Any] = self._load_catalog()

    def _resolve_provider_name(self) -> str:
        """Determine the catalog provider name from config.

        Priority:
        1. ``catalog_sync.provider`` key
        2. If ``aibox_catalog_sync`` exists in config, provider is "aibox"
        3. Fall back to first upstream name
        """
        sync_config = self.config.get("catalog_sync", {})
        if isinstance(sync_config, dict) and sync_config.get("provider"):
            return str(sync_config["provider"])
        if "aibox_catalog_sync" in self.config:
            return "aibox"
        upstreams = self.config.get("upstreams", {})
        if upstreams:
            return next(iter(upstreams))
        return "unknown"

    def _get_sync_config(self) -> dict[str, Any]:
        """Read catalog sync config with backward compat fallback."""
        config = self.config.get("catalog_sync", {})
        if isinstance(config, dict) and config:
            return config
        # Backward compat: legacy aibox_catalog_sync key
        config = self.config.get("aibox_catalog_sync", {})
        if isinstance(config, dict):
            return config
        return {}

    def _get_policy(self) -> dict[str, Any]:
        """Read auto-promotion policy with backward compat fallback."""
        policy = self.config.get("catalog_auto_promotion", {})
        if isinstance(policy, dict) and policy:
            return policy
        # Backward compat: legacy aibox_auto_promotion key
        policy = self.config.get("aibox_auto_promotion", {})
        if isinstance(policy, dict):
            return policy
        return {}

    def _state_path(self, setting: str) -> Path:
        sync_config = self._get_sync_config()
        value = sync_config.get(setting)
        if not value:
            from router import RouterConfigurationError
            raise RouterConfigurationError(f"missing catalog setting: {setting}")
        path = Path(str(value))
        return path if path.is_absolute() else self.base_dir / path

    def _load_catalog(self) -> dict[str, Any]:
        try:
            path = self._state_path("state_file")
        except Exception:
            return {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            selected = value.get("selected_routes", {})
            if isinstance(value, dict) and isinstance(selected, dict):
                # Routes will be applied when router calls _apply_generated_routes
                pass
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def apply_generated_routes(self, routes: dict[str, dict[str, Any]], selected: dict[str, list[str]]) -> None:
        """Apply generated catalog routes to the router's route dict.

        Route names use the provider name from config, not hardcoded.
        """
        provider = self._resolve_provider_name()
        routes[f"claude-router-{provider}-cheap"] = {
            "strategy": "priority",
            "generated": True,
            "candidates": self._make_candidates(selected.get("cheap", []), provider),
        }
        routes[f"claude-router-{provider}-engineering"] = {
            "strategy": "priority",
            "generated": True,
            "candidates": self._make_candidates(selected.get("engineering", []), provider),
        }
        routes[f"claude-router-{provider}-review"] = {
            "strategy": "priority",
            "generated": True,
            "candidates": self._make_candidates(selected.get("critical_review", []), provider),
        }

    def _make_candidates(self, models: list[str], provider: str) -> list:
        from router import Candidate
        return [Candidate(provider, model) for model in models]

    async def sync_catalog(self, initial: bool = False) -> dict[str, Any]:
        if self.sync_running:
            return {"ok": False, "skipped": True, "reason": "sync already running", "catalog": self.catalog}
        self.sync_running = True
        lock_path = self._state_path("lock_file")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd: int | None = None
        try:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                return {"ok": False, "skipped": True, "reason": "sync lock exists", "catalog": self.catalog}

            provider = self._resolve_provider_name()
            sync_config = self._get_sync_config()

            token_env = str(self._upstream_config_fn(provider).get("auth", {}).get("token_env", f"{provider.upper()}_API_KEY"))
            if not os.getenv(token_env):
                raise CatalogSyncError(f"missing {token_env}")
            docs_url = str(sync_config["docs_url"])
            pricing_url = str(sync_config["pricing_url"])
            models_url = str(sync_config["models_url"])
            pricing_api_url = str(
                sync_config.get("pricing_api_url", f"https://api.{provider}.vn/api/pricing")
            )
            quota_per_usd = float(sync_config.get("quota_per_usd", 500000.0))
            client = self.clients.get(provider)
            if client is None:
                raise CatalogSyncError(f"{provider} client is not initialized")
            docs_text = await self._get_public_text(client, docs_url)
            pricing_text = await self._get_public_text(client, pricing_url)
            newapi_entries: list[dict[str, Any]] = []
            try:
                newapi_payload = json.loads(await self._get_public_text(client, pricing_api_url))
                newapi_entries = (
                    newapi_payload.get("data", []) if isinstance(newapi_payload, dict) else newapi_payload
                )
                if not isinstance(newapi_entries, list):
                    newapi_entries = []
            except (ValueError, json.JSONDecodeError):
                self.logger.warning("pricing_api_url=%s did not return JSON", pricing_api_url)
                newapi_entries = []
            models_response = await client.get(
                models_url,
                headers=self._upstream_headers_fn(provider, {}),
                timeout=self._timeout_fn(catalog=True),
            )
            if models_response.status_code != 200:
                raise CatalogSyncError(f"/v1/models returned {models_response.status_code}")
            payload = models_response.json()
            raw_models = payload.get("data", payload) if isinstance(payload, dict) else payload
            if not isinstance(raw_models, list):
                raise CatalogSyncError("/v1/models response has no model list")
            runtime_models = [str(item.get("id")) for item in raw_models if isinstance(item, dict) and item.get("id")]
            if not runtime_models:
                raise CatalogSyncError("/v1/models returned an empty model list")
            policy = self._get_policy()
            records, public_names = build_records(
                runtime_models,
                docs_text,
                pricing_text,
                policy,
                newapi_entries=newapi_entries,
                quota_per_usd=quota_per_usd,
            )
            if not records:
                raise CatalogSyncError("catalog produced no records")
            selected = select_routes(records, policy)
            timestamp = _now()
            warnings = []
            if any(r.reason == "price unknown" for r in records):
                warnings.append("some model prices are unknown; they were not auto-promoted")
            if selected.get("critical_candidates"):
                warnings.append("ACTION REQUIRED: new critical-review candidate found")
            state = state_from_records(records, runtime_models, public_names, selected, timestamp, warnings)
            self._persist_catalog(state, selected)
            self.catalog = state
            self.logger.info(
                "catalog sync ok verified=%s cheap=%s engineering=%s critical_candidates=%s",
                len(state["verified_models"]),
                len(selected["cheap"]),
                len(selected["engineering"]),
                len(selected["critical_candidates"]),
            )
            return {"ok": True, "initial": initial, "catalog": state}
        except (httpx.HTTPError, ValueError, CatalogSyncError) as exc:
            self.logger.warning("catalog sync failed: %s; keeping last-known-good", _safe_error(exc))
            return {"ok": False, "initial": initial, "error": _safe_error(exc), "catalog": self.catalog}
        except Exception as exc:
            from router import RouterConfigurationError
            if isinstance(exc, RouterConfigurationError):
                self.logger.warning("catalog sync failed: %s; keeping last-known-good", _safe_error(exc))
                return {"ok": False, "initial": initial, "error": _safe_error(exc), "catalog": self.catalog}
            raise
        finally:
            if fd is not None:
                os.close(fd)
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.sync_running = False

    async def _get_public_text(self, client: httpx.AsyncClient, url: str) -> str:
        response = await client.get(url, timeout=self._timeout_fn(catalog=True))
        if response.status_code != 200:
            raise CatalogSyncError(f"{url} returned {response.status_code}")
        return response.text

    def _persist_catalog(self, state: dict[str, Any], selected: dict[str, list[str]]) -> None:
        state_path = self._state_path("state_file")
        previous_path = self._state_path("previous_state_file")
        generated_path = self._state_path("generated_routes_file")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if state_path.exists():
            shutil.copyfile(state_path, previous_path)
        _atomic_json(state_path, state)
        provider = self._resolve_provider_name()
        generated = {
            f"claude-router-{provider}-cheap": {"strategy": "priority", "candidates": selected.get("cheap", [])},
            f"claude-router-{provider}-engineering": {"strategy": "priority", "candidates": selected.get("engineering", [])},
            f"claude-router-{provider}-review": {"strategy": "priority", "candidates": selected.get("critical_review", [])},
        }
        _atomic_yaml(generated_path, generated)


def _safe_error(exc: BaseException) -> str:
    import re
    message = str(exc).replace("\n", " ").strip()
    return re.sub(r"(authorization|api[-_]?key|token)=?\S+", r"\1=<redacted>", message, flags=re.IGNORECASE)[:240] or exc.__class__.__name__
