"""Local Anthropic-compatible smart router for ProxyPal and AI-BOX."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from apps.gateway.api.admin import router as admin_router

# Moved to worker/collectors - import removed


BASE_DIR = Path(__file__).resolve().parent
FAILOVER_STATUSES = {408, 429, 500, 502, 503, 504}
NON_RETRYABLE_CLIENT_STATUSES = {400, 401, 403}
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
FORWARD_EXACT_HEADERS = {"accept", "cache-control", "content-type", "user-agent"}


class RouterConfigurationError(RuntimeError):
    pass


class CatalogSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    upstream: str
    model: str
    weight: int = 1

    @property
    def key(self) -> str:
        return f"{self.upstream}:{self.model}"


@dataclass
class CircuitState:
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_status: int | None = None
    last_error: str | None = None
    last_event: str | None = None


@dataclass
class OpenStream:
    context_manager: Any
    response: httpx.Response
    iterator: AsyncIterator[bytes]
    first_chunk: bytes
    candidate: Candidate


class SmartRouter:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.routes: dict[str, dict[str, Any]] = {}
        for name, route in config.get("routes", {}).items():
            self.routes[name] = self._parse_route(route)
        self.clients: dict[str, httpx.AsyncClient] = {}
        self.circuits: dict[str, CircuitState] = {}
        self.rr_current: dict[str, dict[str, float]] = {}
        self.state_lock = asyncio.Lock()
        self.sync_task: asyncio.Task[None] | None = None
        self.sync_running = False
        self.catalog: dict[str, Any] = self._load_catalog()
        self.logger = logging.getLogger("smart-router")
        self._configure_logging()

    @classmethod
    def from_environment(cls) -> "SmartRouter":
        config_value = os.getenv("SMART_ROUTER_CONFIG", "config.yaml")
        path = Path(config_value)
        if not path.is_absolute():
            path = BASE_DIR / path
        try:
            with path.open("r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RouterConfigurationError(f"unable to load router config: {exc}") from exc
        if not isinstance(config, dict):
            raise RouterConfigurationError("router config must be a YAML mapping")
        instance = cls(config)
        instance.config_path = path
        return instance

    def _configure_logging(self) -> None:
        level_name = str(self.config.get("logging", {}).get("level", "INFO")).upper()
        self.logger.setLevel(getattr(logging, level_name, logging.INFO))
        if self.logger.handlers:
            return
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        self.logger.addHandler(console)
        log_value = self.config.get("logging", {}).get("file")
        if log_value:
            log_path = Path(log_value)
            if not log_path.is_absolute():
                log_path = BASE_DIR / log_path
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

    @staticmethod
    def _parse_candidates(items: Any) -> list[Candidate]:
        candidates = []
        for item in items or []:
            if not isinstance(item, dict) or not item.get("upstream") or not item.get("model"):
                continue
            candidates.append(
                Candidate(
                    upstream=str(item["upstream"]),
                    model=str(item["model"]),
                    weight=max(1, int(item.get("weight", 1))),
                )
            )
        return candidates

    @staticmethod
    def _parse_route(route: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy": str(route.get("strategy", "priority")),
            "candidates": SmartRouter._parse_candidates(route.get("candidates")),
            "fallback": SmartRouter._parse_candidates(route.get("fallback")),
            "generated": bool(route.get("generated", False)),
        }

    async def start(self) -> None:
        timeout = self._timeout()
        self.clients = {
            name: httpx.AsyncClient(http2=True, follow_redirects=False, timeout=timeout)
            for name in self.config.get("upstreams", {})
        }
        sync_config = self.config.get("aibox_catalog_sync", {})
        if sync_config.get("enabled", False):
            await self.sync_catalog(initial=True)
            self.sync_task = asyncio.create_task(self._sync_loop(), name="aibox-catalog-sync")
        self.logger.info("router started on 127.0.0.1:8320")

    async def close(self) -> None:
        if self.sync_task is not None:
            self.sync_task.cancel()
            try:
                await self.sync_task
            except asyncio.CancelledError:
                pass
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()
        self.logger.info("router stopped")

    async def _sync_loop(self) -> None:
        interval = max(60, int(self.config.get("aibox_catalog_sync", {}).get("interval_seconds", 21600)))
        while True:
            await asyncio.sleep(interval)
            await self.sync_catalog()

    def authorize(self, request: Request) -> None:
        expected = os.getenv(str(self.config.get("server", {}).get("router_key_env", "SMART_ROUTER_KEY")), "")
        if not expected:
            raise HTTPException(status_code=503, detail="router authentication is not configured")
        authorization = request.headers.get("authorization", "")
        presented = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
        presented = presented or request.headers.get("x-api-key", "")
        if not presented or not _constant_time_equal(presented, expected):
            raise HTTPException(status_code=401, detail="invalid router credential")

    def _upstream_config(self, name: str) -> dict[str, Any]:
        upstream = self.config.get("upstreams", {}).get(name)
        if not isinstance(upstream, dict):
            raise RouterConfigurationError(f"unknown upstream: {name}")
        return upstream

    def _upstream_headers(self, name: str, incoming: Any) -> dict[str, str]:
        upstream = self._upstream_config(name)
        auth = upstream.get("auth", {})
        token_env = str(auth.get("token_env", ""))
        token = os.getenv(token_env, "") if token_env else ""
        if not token:
            raise RouterConfigurationError(f"missing credential environment variable: {token_env}")
        forwarded: dict[str, str] = {}
        for key, value in incoming.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"authorization", "x-api-key"}:
                continue
            if lower.startswith("anthropic-") or lower.startswith("x-claude-code-") or lower in FORWARD_EXACT_HEADERS:
                forwarded[key] = value
        mode = str(auth.get("mode", "bearer")).lower()
        if mode == "x-api-key":
            forwarded["x-api-key"] = token
        else:
            forwarded["authorization"] = f"Bearer {token}"
        return forwarded

    def _timeout(self, catalog: bool = False) -> httpx.Timeout:
        settings = self.config.get("http", {})
        if catalog:
            read = float(settings.get("catalog_timeout_seconds", 20))
        else:
            read = float(settings.get("read_timeout_seconds", 300))
        return httpx.Timeout(
            connect=float(settings.get("connect_timeout_seconds", 10)),
            read=read,
            write=float(settings.get("write_timeout_seconds", 60)),
            pool=float(settings.get("pool_timeout_seconds", 10)),
        )

    def _url(self, upstream: str, path: str) -> str:
        base = str(self._upstream_config(upstream).get("base_url", "")).rstrip("/")
        return f"{base}{path}"

    async def _candidate_order(self, route_name: str) -> list[Candidate]:
        async with self.state_lock:
            route = self.routes.get(route_name)
            if route is None:
                return []
            # Primary candidates are ordered by the route strategy; fallback
            # candidates are only reached after every primary candidate has been
            # tried and failed (e.g. proxypal exhausted -> aibox backup).
            primary = [c for c in route["candidates"] if self._is_available(c)]
            if route["strategy"] == "smooth_weighted_rr" and len(primary) >= 2:
                current = self.rr_current.setdefault(route_name, {})
                total = sum(c.weight for c in primary)
                for candidate in primary:
                    current[candidate.key] = current.get(candidate.key, 0.0) + candidate.weight
                selected = max(primary, key=lambda item: current.get(item.key, 0.0))
                current[selected.key] -= total
                remaining = [candidate for candidate in primary if candidate != selected]
                remaining.sort(key=lambda item: current.get(item.key, 0.0), reverse=True)
                ordered = [selected, *remaining]
            else:
                ordered = list(primary)
            fallback = [c for c in route.get("fallback", []) if self._is_available(c)]
            return ordered + fallback

    def _is_available(self, candidate: Candidate) -> bool:
        state = self.circuits.get(candidate.key)
        return state is None or state.cooldown_until <= time.monotonic()

    async def handle_messages(self, body: dict[str, Any], incoming_headers: Any, path: str) -> Response:
        route_name = body.get("model")
        if not isinstance(route_name, str) or route_name not in self.routes:
            return JSONResponse(
                status_code=404,
                content={"error": {"type": "unknown_model", "message": "unknown logical router model"}},
            )
        candidates = await self._candidate_order(route_name)
        if not candidates:
            return JSONResponse(
                status_code=503,
                content={"error": {"type": "overloaded", "message": "all candidates are cooling down or unavailable"}},
            )
        if bool(body.get("stream", False)) and path.endswith("/messages"):
            return await self._stream_messages(body, incoming_headers, candidates, route_name, path)
        return await self._non_stream_messages(body, incoming_headers, candidates, route_name, path)

    async def _non_stream_messages(
        self,
        body: dict[str, Any],
        incoming_headers: Any,
        candidates: list[Candidate],
        route_name: str,
        path: str,
    ) -> Response:
        last_failure: str | None = None
        for candidate in candidates:
            request_body = dict(body)
            request_body["model"] = candidate.model
            try:
                headers = self._upstream_headers(candidate.upstream, incoming_headers)
                client = self.clients[candidate.upstream]
                response = await client.post(self._url(candidate.upstream, path), headers=headers, json=request_body)
            except (httpx.RequestError, RouterConfigurationError) as exc:
                last_failure = _safe_error(exc)
                await self._record_failure(candidate, None, last_failure)
                continue
            if response.status_code in FAILOVER_STATUSES:
                last_failure = f"upstream returned {response.status_code}"
                await self._record_failure(candidate, response.status_code, last_failure)
                continue
            await self._record_success(candidate, response.status_code)
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=_response_headers(response.headers),
            )
        self.logger.warning("route=%s failover_exhausted reason=%s", route_name, last_failure or "unknown")
        return JSONResponse(
            status_code=503,
            content={"error": {"type": "upstream_unavailable", "message": last_failure or "no candidate succeeded"}},
        )

    async def _stream_messages(
        self,
        body: dict[str, Any],
        incoming_headers: Any,
        candidates: list[Candidate],
        route_name: str,
        path: str,
    ) -> Response:
        last_failure: str | None = None
        for candidate in candidates:
            request_body = dict(body)
            request_body["model"] = candidate.model
            opened = await self._open_stream(candidate, request_body, incoming_headers, path)
            if isinstance(opened, Response):
                if opened.status_code in FAILOVER_STATUSES:
                    last_failure = f"upstream returned {opened.status_code}"
                    await self._record_failure(candidate, opened.status_code, last_failure)
                    continue
                return opened
            stream = opened
            response_headers = _response_headers(stream.response.headers)

            async def iterator() -> AsyncIterator[bytes]:
                try:
                    if stream.first_chunk:
                        yield stream.first_chunk
                    async for chunk in stream.iterator:
                        yield chunk
                    await self._record_success(candidate, stream.response.status_code)
                except Exception as exc:
                    await self._record_failure(candidate, None, _safe_error(exc))
                    raise
                finally:
                    await stream.context_manager.__aexit__(None, None, None)

            return StreamingResponse(iterator(), status_code=stream.response.status_code, headers=response_headers)
        self.logger.warning("route=%s stream_failover_exhausted reason=%s", route_name, last_failure or "unknown")
        return JSONResponse(
            status_code=503,
            content={"error": {"type": "upstream_unavailable", "message": last_failure or "no candidate succeeded"}},
        )

    async def _open_stream(self, candidate: Candidate, body: dict[str, Any], incoming_headers: Any, path: str) -> OpenStream | Response:
        try:
            headers = self._upstream_headers(candidate.upstream, incoming_headers)
            client = self.clients[candidate.upstream]
            context_manager = client.stream("POST", self._url(candidate.upstream, path), headers=headers, json=body)
            response = await context_manager.__aenter__()
        except (httpx.RequestError, RouterConfigurationError) as exc:
            await self._record_failure(candidate, None, _safe_error(exc))
            return JSONResponse(status_code=503, content={"error": {"type": "upstream_unavailable", "message": "connection failed"}})

        if response.status_code >= 400:
            content = await response.aread()
            await context_manager.__aexit__(None, None, None)
            return Response(content=content, status_code=response.status_code, headers=_response_headers(response.headers))

        iterator = response.aiter_bytes()
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            first = b""
        except httpx.RequestError as exc:
            await context_manager.__aexit__(None, None, None)
            await self._record_failure(candidate, None, _safe_error(exc))
            return JSONResponse(status_code=503, content={"error": {"type": "upstream_unavailable", "message": "stream failed before output"}})
        return OpenStream(context_manager, response, iterator, first, candidate)

    async def _record_success(self, candidate: Candidate, status: int | None) -> None:
        async with self.state_lock:
            state = self.circuits.setdefault(candidate.key, CircuitState())
            state.cooldown_until = 0.0
            state.consecutive_failures = 0
            state.last_status = status
            state.last_error = None
            state.last_event = _now()
        self.logger.info("upstream=%s model=%s status=%s failover=false", candidate.upstream, candidate.model, status)

    async def _record_failure(self, candidate: Candidate, status: int | None, error: str) -> None:
        if status in NON_RETRYABLE_CLIENT_STATUSES:
            return
        async with self.state_lock:
            state = self.circuits.setdefault(candidate.key, CircuitState())
            state.consecutive_failures += 1
            state.last_status = status
            state.last_error = error[:240]
            state.last_event = _now()
            if candidate.upstream == "aibox":
                if status == 429:
                    cooldown = 300
                else:
                    cooldown = min(120, 30 * (2 ** max(0, state.consecutive_failures - 1)))
            else:
                if status == 429:
                    cooldown = 120
                else:
                    cooldown = min(120, 15 * (2 ** max(0, state.consecutive_failures - 1)))
            state.cooldown_until = time.monotonic() + cooldown
        self.logger.warning(
            "upstream=%s model=%s status=%s cooldown=%ss failover=true",
            candidate.upstream,
            candidate.model,
            status,
            cooldown,
        )

    def status_payload(self) -> dict[str, Any]:
        now = time.monotonic()
        routes: list[dict[str, Any]] = []
        for route_name, route in self.routes.items():
            entries = [(candidate, "primary") for candidate in route["candidates"]] + [
                (candidate, "fallback") for candidate in route.get("fallback", [])
            ]
            for candidate, role in entries:
                state = self.circuits.get(candidate.key, CircuitState())
                routes.append(
                    {
                        "route": route_name,
                        "role": role,
                        "upstream": candidate.upstream,
                        "model": candidate.model,
                        "available": state.cooldown_until <= now,
                        "cooldown_remaining": max(0, round(state.cooldown_until - now, 2)),
                        "consecutive_failures": state.consecutive_failures,
                        "last_status": state.last_status,
                        "last_error": state.last_error,
                        "last_event": state.last_event,
                    }
                )
        return {"routes": routes, "catalog_last_successful_sync": self.catalog.get("last_successful_sync")}

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

            token_env = str(self._upstream_config("aibox").get("auth", {}).get("token_env", "AIBOX_API_KEY"))
            if not os.getenv(token_env):
                raise CatalogSyncError(f"missing {token_env}")
            docs_url = str(self.config["aibox_catalog_sync"]["docs_url"])
            pricing_url = str(self.config["aibox_catalog_sync"]["pricing_url"])
            models_url = str(self.config["aibox_catalog_sync"]["models_url"])
            sync_args = self.config.get("aibox_catalog_sync", {})
            pricing_api_url = str(
                sync_args.get("pricing_api_url", "https://api.ai-box.vn/api/pricing")
            )
            quota_per_usd = float(sync_args.get("quota_per_usd", 500000.0))
            client = self.clients.get("aibox")
            if client is None:
                raise CatalogSyncError("AI-BOX client is not initialized")
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
                headers=self._upstream_headers("aibox", {}),
                timeout=self._timeout(catalog=True),
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
            policy = self.config.get("aibox_auto_promotion", {})
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
            self._apply_generated_routes(selected)
            self.logger.info(
                "catalog sync ok verified=%s cheap=%s engineering=%s critical_candidates=%s",
                len(state["verified_models"]),
                len(selected["cheap"]),
                len(selected["engineering"]),
                len(selected["critical_candidates"]),
            )
            return {"ok": True, "initial": initial, "catalog": state}
        except (httpx.HTTPError, ValueError, CatalogSyncError, RouterConfigurationError) as exc:
            self.logger.warning("catalog sync failed: %s; keeping last-known-good", _safe_error(exc))
            return {"ok": False, "initial": initial, "error": _safe_error(exc), "catalog": self.catalog}
        finally:
            if fd is not None:
                os.close(fd)
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.sync_running = False

    async def _get_public_text(self, client: httpx.AsyncClient, url: str) -> str:
        response = await client.get(url, timeout=self._timeout(catalog=True))
        if response.status_code != 200:
            raise CatalogSyncError(f"{url} returned {response.status_code}")
        return response.text

    def _apply_generated_routes(self, selected: dict[str, list[str]]) -> None:
        self.routes["claude-router-aibox-cheap"] = {
            "strategy": "priority",
            "generated": True,
            "candidates": [Candidate("aibox", model) for model in selected.get("cheap", [])],
        }
        self.routes["claude-router-aibox-engineering"] = {
            "strategy": "priority",
            "generated": True,
            "candidates": [Candidate("aibox", model) for model in selected.get("engineering", [])],
        }
        self.routes["claude-router-aibox-review"] = {
            "strategy": "priority",
            "generated": True,
            "candidates": [Candidate("aibox", model) for model in selected.get("critical_review", [])],
        }

    def _state_path(self, setting: str) -> Path:
        value = self.config.get("aibox_catalog_sync", {}).get(setting)
        if not value:
            raise RouterConfigurationError(f"missing catalog setting: {setting}")
        path = Path(str(value))
        return path if path.is_absolute() else BASE_DIR / path

    def _load_catalog(self) -> dict[str, Any]:
        try:
            path = self._state_path("state_file")
        except RouterConfigurationError:
            return {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            selected = value.get("selected_routes", {})
            if isinstance(value, dict) and isinstance(selected, dict):
                self._apply_generated_routes(selected)
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _persist_catalog(self, state: dict[str, Any], selected: dict[str, list[str]]) -> None:
        state_path = self._state_path("state_file")
        previous_path = self._state_path("previous_state_file")
        generated_path = self._state_path("generated_routes_file")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if state_path.exists():
            shutil.copyfile(state_path, previous_path)
        _atomic_json(state_path, state)
        generated = {
            "claude-router-aibox-cheap": {"strategy": "priority", "candidates": selected.get("cheap", [])},
            "claude-router-aibox-engineering": {"strategy": "priority", "candidates": selected.get("engineering", [])},
            "claude-router-aibox-review": {"strategy": "priority", "candidates": selected.get("critical_review", [])},
        }
        _atomic_yaml(generated_path, generated)


def _constant_time_equal(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    result = 0
    for a, b in zip(left.encode(), right.encode()):
        result |= a ^ b
    return result == 0


def _safe_error(exc: BaseException) -> str:
    message = str(exc).replace("\n", " ").strip()
    return re.sub(r"(authorization|api[-_]?key|token)=?\S+", r"\1=<redacted>", message, flags=re.IGNORECASE)[:240] or exc.__class__.__name__


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_yaml(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(value, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def get_authorized_service(request: Request) -> SmartRouter:
    service: SmartRouter = request.app.state.router
    service.authorize(request)
    return service


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = SmartRouter.from_environment()
    app.state.router = service
    await service.start()
    try:
        yield
    finally:
        await service.close()


app = FastAPI(title="Claude Smart Router", version="1.0.0", lifespan=lifespan)
app.include_router(admin_router, prefix="/api/admin/v1")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health/live")
async def health_live() -> dict[str, Any]:
    return {"status": "ok", "service": "smart-router"}


@app.get("/health/ready")
async def health_ready(request: Request) -> dict[str, Any]:
    service = getattr(request.app.state, "router", None)
    upstream_count = len(service.clients) if service is not None else 0
    return {"status": "ok", "service": "smart-router", "upstream_count": upstream_count}


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    service: SmartRouter = request.app.state.router
    return {"status": "ok", "service": "smart-router", "upstream_count": len(service.clients)}


@app.get("/v1/models")
async def models(service: SmartRouter = Depends(get_authorized_service)) -> dict[str, Any]:
    data = [
        {"id": name, "object": "model", "owned_by": "smart-router", "display_name": name}
        for name in service.routes
    ]
    return {"object": "list", "data": data}


@app.post("/v1/messages")
async def messages(request: Request, service: SmartRouter = Depends(get_authorized_service)) -> Response:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return await service.handle_messages(body, request.headers, "/v1/messages")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request, service: SmartRouter = Depends(get_authorized_service)) -> Response:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return await service.handle_messages(body, request.headers, "/v1/messages/count_tokens")


@app.post("/router/aibox/sync")
async def force_aibox_sync(service: SmartRouter = Depends(get_authorized_service)) -> Response:
    result = await service.sync_catalog()
    return JSONResponse(status_code=200 if result.get("ok") else 502, content=result)


@app.get("/router/aibox/catalog")
async def aibox_catalog(service: SmartRouter = Depends(get_authorized_service)) -> dict[str, Any]:
    return {
        "catalog": service.catalog,
        "routes": {
            name: [candidate.model for candidate in route["candidates"]]
            for name, route in service.routes.items()
            if route.get("generated")
        },
    }


@app.get("/router/status")
async def router_status(service: SmartRouter = Depends(get_authorized_service)) -> dict[str, Any]:
    return service.status_payload()
