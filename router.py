"""Local Anthropic-compatible smart router for ProxyPal and AI-BOX."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import time
import types
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from apps.gateway.api.admin import router as admin_router
from apps.gateway.db.session import dispose_engine
from apps.gateway.routing.engine import RouterEngine
from apps.gateway.config.compiler import LegacyConfigCompiler
from apps.gateway.usage.ledger import AttemptRecord, RequestRecord
from apps.gateway.routing.scoring import (
    ScoringConfig, ScoringWeights, SmartScoreCalculator,
    RollingFailureRateTracker, LatencyTracker, SessionAffinityStore,
    CandidateMetrics,
)
from apps.worker.collectors.aibox_catalog import build_records, select_routes, state_from_records

# ── Quota reservation factory (Redis → InMemory fallback) ─────────────


def _build_quota_reservations():
    """Create quota reservations using REDIS_URL env var; auto-fallback."""
    url = os.getenv("REDIS_URL")
    if not url:
        logger_debug = logging.getLogger("smart-router")
        logger_debug.debug("REDIS_URL not set – using in-memory quota reservations")
        from apps.gateway.quota.reservations import InMemoryQuotaReservations
        from apps.gateway.quota.adapter import AsyncQuotaFacade

        return AsyncQuotaFacade(InMemoryQuotaReservations())
    try:
        from apps.gateway.quota.redis_backend import RedisQuotaReservations  # type: ignore[import-not-found]
        rqr = RedisQuotaReservations(url)
        logger_debug = logging.getLogger("smart-router")
        logger_debug.info("Connected to Redis for distributed quota reservations (%s)", url)
        return rqr
    except Exception as exc:  # noqa: BLE001
        logger_warn = logging.getLogger("smart-router")
        logger_warn.warning(
            "Redis connection failed (%s) – falling back to in-memory quota reservations", exc
        )
        from apps.gateway.quota.reservations import InMemoryQuotaReservations
        from apps.gateway.quota.adapter import AsyncQuotaFacade

        return AsyncQuotaFacade(InMemoryQuotaReservations())


BASE_DIR = Path(__file__).resolve().parent
_REQUEST_USAGE_LEDGER: ContextVar[Any | None] = ContextVar("request_usage_ledger", default=None)
_REQUEST_USAGE_EVENTS: ContextVar[list[Any] | None] = ContextVar("request_usage_events", default=None)
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
    metadata: dict[str, Any] = field(default_factory=dict)

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
    last_kind: str | None = None
    last_scope: str | None = None
    retry_after: str | None = None
    reset_at: str | None = None


@dataclass(frozen=True)
class FailureRuntimeDecision:
    kind: str
    scope: str | None
    record_circuit: bool
    record_scoring_failure: bool
    cooldown_seconds: float | None
    try_next: bool
    quota_observation: bool
    reservation_finalization: str


@dataclass
class OpenStream:
    context_manager: Any
    response: httpx.Response
    iterator: AsyncIterator[bytes]
    first_chunk: bytes
    candidate: Candidate


class SmartRouter:
    def __init__(self, config: dict[str, Any], quota_reservations=None, usage_ledger=None):
        self.config = config
        self.quota_reservations = quota_reservations
        self._usage_ledger = usage_ledger
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
        # ── Smart scoring subsystem ──────────────────────────────────────
        scoring_data = config.get("smart_scheduler", {})
        self._scoring_config = ScoringConfig.from_dict(scoring_data) if scoring_data else ScoringConfig()
        self._failure_tracker = RollingFailureRateTracker(self._scoring_config.max_failure_history)
        self._latency_tracker = LatencyTracker(self._scoring_config.max_latency_history)
        self._session_store = SessionAffinityStore(self._scoring_config.session_affinity_ttl_seconds)
        self._score_calculator: SmartScoreCalculator | None = None
        self.router_engine = None
        if os.getenv("USE_ROUTER_ENGINE", "false").lower() == "true":
            compiler = LegacyConfigCompiler()
            snapshot = compiler.compile_dict(config)
            engine_quota = quota_reservations
            # Ensure RouterEngine gets an async-compatible backend too.
            if engine_quota is not None and not asyncio.iscoroutinefunction(
                getattr(engine_quota, "check_many", None),
            ):
                from apps.gateway.quota.adapter import AsyncQuotaFacade
                engine_quota = AsyncQuotaFacade(engine_quota)
            self.router_engine = RouterEngine(
                snapshot, quota_reservations=engine_quota, scoring_config=self._scoring_config,
            )
        # Ensure our own quota_reservations is async-compatible.
        if (self.quota_reservations is not None
                and not asyncio.iscoroutinefunction(getattr(self.quota_reservations, "check_many", None))):
            from apps.gateway.quota.adapter import AsyncQuotaFacade
            self.quota_reservations = AsyncQuotaFacade(self.quota_reservations)  # type: ignore[assignment]
        self.driver_registry = None

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
        quota_reservations = _build_quota_reservations()
        instance = cls(config, quota_reservations=quota_reservations)
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
                    metadata=SmartRouter._candidate_metadata(item),
                )
            )
        return candidates

    @staticmethod
    def _candidate_metadata(item: dict[str, Any]) -> dict[str, Any]:
        metadata = {}
        for key in ("quota_resource_id", "quota_resource_ids"):
            if key in item:
                metadata[key] = item[key]
        # Chuyển tiếp metadata cho policy preset (AC-09/AC-10) — không branch provider
        for key in ("quality_score", "is_paid", "expected_cost_per_request"):
            if key in item:
                metadata[key] = item[key]
        # Smart scoring hints
        for key in ("session_group", "driver_id"):
            val = item.get(key)
            if val is not None:
                metadata[key] = val
        # M5 feature metrics: giữ để scoring 4 chiều mới có dữ liệu, fail-open nếu thiếu
        for key in (
            "expiry_urgency", "scarcity", "retry_expected_cost_per_request", "uncertainty_score",
            "capabilities", "max_context_tokens", "context_window",
            "concurrency_used", "concurrency_limit", "concurrent_requests", "max_concurrency", "inflight",
            "enabled", "deprecated", "model_state", "state", "circuit_state",
            "credential_state", "auth_state", "protocol", "protocols",
            "project_budget_state", "budget_state", "project_budget_exhausted", "budget_exhausted",
            "max_output_tokens", "max_output",
        ):
            if key in item:
                metadata[key] = item[key]
        return metadata

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
        # Hydrate quota resources from the database so running routers have
        # fresh quota state on startup (covers Redis and InMemory backends).
        await self._hydrate_quota_from_db()

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

    async def _hydrate_quota_from_db(self) -> None:
        """Load quota resources from the database into the running backend.

        Opens a one-shot async session via ``get_async_session_factory``,
        calls ``QuotaResourceRepository.list_resources()``, and adds each
        resource to ``self.quota_reservations`` (if available).  Gracefully
        skips when no DB engine has been initialised.
        """
        if self.quota_reservations is None:
            return

        # Determine if the resource ID is an async method so we can await it.
        _add = getattr(self.quota_reservations, "add_resource", None)
        needs_await = asyncio.iscoroutinefunction(_add) if _add else False

        try:
            from apps.gateway.db.session import get_async_session_factory as _get_factory
            from apps.gateway.quota.reservations import QuotaResourceRepository as _Repo

            factory = _get_factory()
            async with factory() as session:
                repo = _Repo(session)
                resources = await repo.list_resources()
        except Exception as exc:  # noqa: BLE001
            # No DB configured or connection failure → skip silently; the
            # router was already built with in-memory defaults that are
            # functionally equivalent for single-process use-cases.
            self.logger.warning(
                "quota hydrate skipped (%s) – using in-memory defaults", exc,
            )
            return

        count = 0
        for res in resources:
            try:
                if needs_await:
                    await self.quota_reservations.add_resource(res)
                else:
                    self.quota_reservations.add_resource(res)
                count += 1
            except Exception as exc:
                self.logger.warning("quota hydrate: skipping %s (%s)", res.resource_id, exc)
        if count:
            self.logger.info("quota hydrated %d resource(s) from database", count)

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

    def _conversation_thread_hint(self, body: dict[str, Any] | None, incoming_headers: Any | None) -> str | None:
        """Trích session hint một cách an toàn — chỉ lưu ID, không lưu prompt/secret.

        Header ưu tiên hơn body. Chấp nhận `x-session-id`, `x-conversation-id`,
        `session_id`, `conversation_id`, `thread_id`. Trả về None nếu thiếu/empty.
        """
        candidate: str | None = None
        if incoming_headers is not None:
            try:
                # Headers có thể là Starlette Headers hoặc dict thường
                get = getattr(incoming_headers, "get", None)
                if callable(get):
                    for key in ("x-session-id", "x-conversation-id", "x-thread-id"):
                        value = get(key)  # type: ignore[call-arg]
                        if isinstance(value, str) and value.strip():
                            return value.strip()
                        # Starlette headers are case-insensitive but dict .get is not — try lower?
                        if isinstance(incoming_headers, dict):
                            # check lowercased keys already via get above with original case;
                            # dict fallback: try case-insensitive scan once
                            pass
                    # Fallback case-insensitive scan for dict headers
                    if isinstance(incoming_headers, dict):
                        lower_map = {str(k).lower(): v for k, v in incoming_headers.items()}
                        for key in ("x-session-id", "x-conversation-id", "x-thread-id"):
                            value = lower_map.get(key)
                            if isinstance(value, str) and value.strip():
                                return value.strip()
                # else: no .get -> ignore
            except Exception:
                pass
        if isinstance(body, dict):
            for key in ("session_id", "conversation_id", "thread_id", "sessionId", "conversationId"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    def _remember_session_affinity(self, conversation_thread: str | None, candidate_key: str) -> None:
        if not conversation_thread:
            return
        self._ensure_scoring()
        if self._score_calculator is None:
            return
        if getattr(self.router_engine, "_score_calculator", None) is not None and hasattr(self.router_engine, "_score_calculator"):
            # Engine có store riêng khi bật RouterEngine mode — đồng bộ cả hai
            try:
                eng_calc = self.router_engine._score_calculator  # type: ignore[union-attr]
                if eng_calc is not None:
                    eng_calc.remember_affinity(conversation_thread, candidate_key)
            except Exception:
                pass
        try:
            self._score_calculator.remember_affinity(conversation_thread, candidate_key)
        except Exception:
            pass

    async def _candidate_order(
        self,
        route_name: str,
        conversation_thread: str | None = None,
        required_capabilities: dict[str, Any] | None = None,
    ) -> list[Candidate]:
        if self.router_engine is not None:
            if getattr(self.router_engine, "quota_reservations", None) is not None:
                resource_candidates = await self.router_engine.select_candidates_async(
                    route_name, conversation_thread=conversation_thread,
                    required_capabilities=required_capabilities,
                )
            else:
                resource_candidates = self.router_engine.select_candidates(
                    route_name, conversation_thread=conversation_thread,
                    required_capabilities=required_capabilities,
                )
            return self._convert_resource_candidates(resource_candidates)
        async with self.state_lock:
            route = self.routes.get(route_name)
            if route is None:
                return []
            # Primary candidates are ordered by the route strategy; fallback
            # candidates are only reached after every primary candidate has been
            # tried and failed (e.g. proxypal exhausted -> aibox backup).
            primary = await self._quota_available_candidates(
                [c for c in route["candidates"] if self._is_available(c)]
            )
            fallback = await self._quota_available_candidates(
                [c for c in route.get("fallback", []) if self._is_available(c)]
            )
        # Policy constraints: hard filter per group (primary vs fallback have
        # different paid-fallback semantics). No-op when scheduler disabled.
        primary = self._apply_policy_constraints(
            primary, route_name, is_fallback=False, required_capabilities=required_capabilities
        )
        fallback = self._apply_policy_constraints(
            fallback, route_name, is_fallback=True, required_capabilities=required_capabilities
        )
        if route["strategy"] == "smooth_weighted_rr" and len(primary) >= 2:
            async with self.state_lock:
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
        ordered = ordered + fallback
        # Apply smart scoring as an optimization layer (graceful no-op when disabled)
        ordered = await self._async_apply_scoring(ordered, route_name, conversation_thread=conversation_thread)
        return ordered

    async def _quota_available_candidates(self, candidates: list[Candidate]) -> list[Candidate]:
        if self.quota_reservations is None:
            return candidates
        accepted = await asyncio.gather(
            *(self._candidate_quota_accepted(c) for c in candidates)
        )
        return [c for c, ok in zip(candidates, accepted) if ok]

    async def _candidate_quota_accepted(self, candidate: Candidate) -> bool:
        from apps.gateway.quota.reservations import QuotaReservationRequest

        resource_ids = await self._known_candidate_quota_resource_ids(candidate)
        if not resource_ids:
            return True
        admission = await self.quota_reservations.check_many([
            QuotaReservationRequest(resource_id, amount=1)
            for resource_id in resource_ids
        ])
        return admission.accepted

    async def _known_candidate_quota_resource_ids(self, candidate: Candidate) -> list[str]:
        if self.quota_reservations is None:
            return []
        known: list[str] = []
        seen_groups: set[str] = set()
        for resource_id in self._candidate_quota_resource_ids(candidate):
            try:
                resource = await self.quota_reservations.snapshot(resource_id)
            except KeyError:
                continue
            if resource.metric != "requests":
                continue
            group_key = resource.shared_group_id or resource.resource_id
            if group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            known.append(resource_id)
        return known

    def _candidate_quota_resource_ids(self, candidate: Candidate) -> list[str]:
        resource_ids = candidate.metadata.get("quota_resource_ids")
        if (
            isinstance(resource_ids, (list, tuple))
            and len(resource_ids) > 0
            and all(isinstance(resource_id, str) and resource_id for resource_id in resource_ids)
        ):
            return list(resource_ids)
        resource_id = candidate.metadata.get("quota_resource_id")
        if isinstance(resource_id, str) and resource_id:
            return [resource_id]
        return [f"model:{candidate.model}"]

    def _convert_resource_candidates(self, resource_candidates: list) -> list[Candidate]:
        candidates = []
        for rc in resource_candidates:
            candidates.append(Candidate(
                upstream=rc.resource_ref.provider_connection_id,
                model=rc.resource_ref.model_id,
                weight=rc.weight,
                metadata=rc.metadata
            ))
        return candidates

    def _is_available(self, candidate: Candidate) -> bool:
        state = self.circuits.get(candidate.key)
        return state is None or state.cooldown_until <= time.monotonic()

    # ── Smart scoring helpers ──────────────────────────────────────────

    def _should_apply_smart_scoring(self, route_name: str) -> bool:
        """Return True when rollout config allows scoring for this route."""
        if not self._scoring_config.enabled:
            return False
        if self._scoring_config.mode not in {"shadow", "active"}:
            return False
        allowlist = getattr(self._scoring_config, "route_allowlist", [])
        return not allowlist or route_name in allowlist

    def _required_capabilities_from_body(self, body: dict[str, Any] | None) -> dict[str, Any]:
        """Suy ra yêu cầu capability từ payload — không đọc secret, chỉ đọc cấu trúc."""
        if not isinstance(body, dict):
            return {}
        caps: dict[str, Any] = {}
        tools = body.get("tools")
        if isinstance(tools, list) and len(tools) > 0:
            caps["tools"] = True
        elif tools:
            caps["tools"] = True
        if body.get("tool_choice"):
            caps["tools"] = True
        messages = body.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        t = block.get("type")
                        if t in ("image", "image_url"):
                            caps["vision"] = True
                            break
                        if t == "image" and isinstance(block.get("source"), dict):
                            caps["vision"] = True
                            break
                    if caps.get("vision"):
                        break
        # Context window: ước lượng thô từ body + max_tokens
        max_tokens = body.get("max_tokens")
        if max_tokens is None:
            max_tokens = body.get("max_output_tokens")
        try:
            import json as _json

            txt = _json.dumps(body, ensure_ascii=False)
            est_input = max(0, len(txt) // 4)
            needed = est_input
            if max_tokens is not None:
                needed += int(max_tokens) + 1000
            else:
                needed += 4000
            if needed > 30000 and needed > 0:
                caps["min_context_tokens"] = needed
        except Exception:
            pass
        return caps

    def _estimate_input_tokens(self, body: dict[str, Any] | None) -> int:
        """Ước lượng input tokens cho retry budget — không lưu payload."""
        if not isinstance(body, dict):
            return 0
        try:
            import json as _json

            txt = _json.dumps(body, ensure_ascii=False)
            return max(0, len(txt) // 4)
        except Exception:
            return 0

    def _apply_policy_constraints(
        self,
        candidates: list[Candidate],
        route_name: str,
        *,
        is_fallback: bool,
        required_capabilities: dict[str, Any] | None = None,
    ) -> list[Candidate]:
        """Lọc hard constraints theo preset, không branch theo provider."""
        # README §18.2 hard state luôn chạy — độc lập với smart scheduler.
        try:
            from apps.gateway.routing.presets import hard_state_eligible
            candidates = [c for c in candidates if hard_state_eligible(c.metadata)]
        except Exception:
            pass
        if not self._should_apply_smart_scoring(route_name):
            return candidates
        try:
            from apps.gateway.routing.presets import filter_candidates_for_policy

            policy = self._scoring_config.effective_preset_for_route(route_name)
            return filter_candidates_for_policy(
                candidates, policy, is_fallback=is_fallback, required_capabilities=required_capabilities
            )
        except Exception:
            return candidates  # Policy lỗi không được chặn data plane

    def _ensure_scoring(self) -> None:
        """Lazy-init the score calculator with catalog and trackers."""
        if self._score_calculator is not None:
            return
        self._score_calculator = SmartScoreCalculator(
            config=self._scoring_config,
            failure_tracker=self._failure_tracker,
            latency_tracker=self._latency_tracker,
            session_store=self._session_store,
            catalog=self.catalog,
        )

    def _apply_smart_scoring(
        self,
        candidates: list[Candidate],
        route_name: str,
        conversation_thread: str | None = None,
    ) -> list[Candidate]:
        """Re-order candidates using multi-dimensional scoring."""
        if not self._should_apply_smart_scoring(route_name):
            return list(candidates)
        self._ensure_scoring()
        if self._score_calculator is None:
            return list(candidates)
        try:
            candidate_keys = [c.key for c in candidates]
            metrics_by_key = self._get_candidate_metrics(candidate_keys)
            route_weights = self._scoring_config.effective_weights_for_route(route_name)
            scored = self._score_calculator.compute_scores(
                candidates=candidates,
                candidate_keys=candidate_keys,
                metrics_by_key=metrics_by_key,
                conversation_thread=conversation_thread,
                weights=route_weights,
            )
            if self._scoring_config.mode == "shadow":
                if self._scoring_config.decision_logging:
                    self.logger.info(
                        "smart scoring shadow route=%s original=%s scored=%s",
                        route_name,
                        [c.key for c in candidates],
                        [c.key for c, _ in scored],
                    )
                return list(candidates)
            return [c for c, _ in scored]
        except Exception as exc:
            self.logger.warning("smart scoring failed for route=%s: %s, preserving order", route_name, exc)
            return list(candidates)

    async def _async_apply_scoring(
        self,
        candidates: list[Candidate],
        route_name: str,
        conversation_thread: str | None = None,
    ) -> list[Candidate]:
        """Async-aware scoring — queries quota resources for burn-rate urgency."""
        if not self._should_apply_smart_scoring(route_name):
            return list(candidates)
        self._ensure_scoring()
        if self._score_calculator is None:
            return list(candidates)
        try:
            candidate_keys = [c.key for c in candidates]
            metrics_by_key = await self._build_candidate_metrics(candidate_keys)
            route_weights = self._scoring_config.effective_weights_for_route(route_name)
            scored = self._score_calculator.compute_scores(
                candidates=candidates,
                candidate_keys=candidate_keys,
                metrics_by_key=metrics_by_key,
                conversation_thread=conversation_thread,
                weights=route_weights,
            )
            if self._scoring_config.mode == "shadow":
                if self._scoring_config.decision_logging:
                    self.logger.info(
                        "smart scoring shadow route=%s original=%s scored=%s",
                        route_name,
                        [c.key for c in candidates],
                        [c.key for c, _ in scored],
                    )
                return list(candidates)
            return [c for c, _ in scored]
        except Exception as exc:
            self.logger.warning("smart scoring failed for route=%s: %s, preserving order", route_name, exc)
            return list(candidates)

    def _get_candidate_metrics(
        self,
        candidate_keys: list[str],
    ) -> dict[str, CandidateMetrics]:
        """Sync-friendly metrics builder — uses quota-agnostic fallback."""
        return self._build_candidate_metrics_sync(candidate_keys)

    async def _async_get_candidate_metrics(
        self,
        candidate_keys: list[str],
    ) -> dict[str, CandidateMetrics]:
        """Async-aware metrics builder — queries quota resources."""
        return await self._build_candidate_metrics(candidate_keys)

    def _build_candidate_metrics_sync(
        self,
        candidate_keys: list[str],
    ) -> dict[str, CandidateMetrics]:
        """Sync metrics builder — skips quota data (uses neutral defaults)."""
        prices = self.catalog.get("prices", {})
        result: dict[str, CandidateMetrics] = {}
        for key in candidate_keys:
            parts = key.split(":", 1)
            model_id = parts[1] if len(parts) > 1 else ""
            price_info = prices.get(model_id, {})
            fail_rate, _, _ = self._failure_tracker.failure_rate(key)
            p50, p99, mean = self._latency_tracker.percentiles(key)
            cb_state = self.circuits.get(key)
            cb_status = "closed"
            consecutive_failures = 0
            if cb_state:
                consecutive_failures = cb_state.consecutive_failures
                if cb_state.cooldown_until > time.monotonic():
                    cb_status = "open"
            result[key] = CandidateMetrics(
                price_per_million_input=price_info.get("input_per_million"),
                price_per_million_output=price_info.get("output_per_million"),
                rolling_failure_rate=fail_rate,
                circuit_breaker_state=cb_status,
                consecutive_failures=consecutive_failures,
                total_attempts=sum(1 for _ in []),  # stub
                total_successes=0,
                p50_latency_ms=p50,
                p99_latency_ms=p99,
                mean_latency_ms=mean,
                request_count=self._latency_tracker.count(key),
                effective_remaining=0,
                limit=0,
                safety_buffer=0,
                burn_rate_urgency=0.0,
                expiry_urgency=0.0,
                scarcity=0.0,
                retry_expected_cost=0.0,
                uncertainty=0.0,
                capability_match=True,
            )
        return result

    async def _build_candidate_metrics(
        self,
        candidate_keys: list[str],
    ) -> dict[str, CandidateMetrics]:
        """Build CandidateMetrics per candidate. Batches quota snapshots by resource_id."""
        prices = self.catalog.get("prices", {})
        resource_ids_for_key: dict[str, list[str]] = {}
        all_resource_ids: set[str] = set()
        for key in candidate_keys:
            parts = key.split(":", 1)
            model_id = parts[1] if len(parts) > 1 else ""
            candidate_for_key = None
            for rname, route in self.routes.items():
                for c in route["candidates"]:
                    if c.key == key:
                        candidate_for_key = c
                        break
                if candidate_for_key:
                    break
            if candidate_for_key is None:
                rid = f"model:{model_id}"
                resource_ids_for_key[key] = [rid]
                all_resource_ids.add(rid)
                continue
            rids = await self._known_candidate_quota_resource_ids(candidate_for_key)
            resource_ids_for_key[key] = rids
            all_resource_ids.update(rids)

        quota_cache: dict[str, Any] = {}
        if self.quota_reservations:
            for rid in all_resource_ids:
                try:
                    quota_cache[rid] = await self.quota_reservations.snapshot(rid)
                except KeyError:
                    pass

        metrics_by_key: dict[str, CandidateMetrics] = {}
        for key in candidate_keys:
            parts = key.split(":", 1)
            model_id = parts[1] if len(parts) > 1 else ""
            price_info = prices.get(model_id, {})
            fail_rate, total_attempts, total_successes = self._failure_tracker.failure_rate(key)
            p50, p99, mean = self._latency_tracker.percentiles(key)
            latency_count = self._latency_tracker.count(key)
            effective_remaining = 0
            limit = 0
            safety_buffer = 0
            burn_urgency = 0.0
            for rid in resource_ids_for_key.get(key, []):
                res = quota_cache.get(rid)
                if res is not None:
                    eff = getattr(res, "effective_remaining", 0)
                    lim = getattr(res, "limit", 0)
                    sb = getattr(res, "safety_buffer", 0)
                    if eff < effective_remaining or effective_remaining == 0:
                        effective_remaining = eff
                    if lim > limit:
                        limit = lim
                    if sb > safety_buffer:
                        safety_buffer = sb
            if limit > 0 and effective_remaining >= 0:
                burn_urgency = 1.0 - (effective_remaining / limit)
            cb_state = self.circuits.get(key)
            cb_status = "closed"
            consecutive_failures = 0
            if cb_state:
                consecutive_failures = cb_state.consecutive_failures
                if cb_state.cooldown_until > time.monotonic():
                    cb_status = "open"
            # M5: lấy 4 chiều mở rộng từ metadata (fail-open, provider-agnostic).
            cand_meta = {}
            for rname, route in self.routes.items():
                for c in route["candidates"]:
                    if c.key == key:
                        cand_meta = c.metadata or {}
                        break
                if cand_meta:
                    break
            metrics_by_key[key] = CandidateMetrics(
                price_per_million_input=price_info.get("input_per_million"),
                price_per_million_output=price_info.get("output_per_million"),
                rolling_failure_rate=fail_rate,
                circuit_breaker_state=cb_status,
                consecutive_failures=consecutive_failures,
                total_attempts=total_attempts,
                total_successes=total_successes,
                p50_latency_ms=p50,
                p99_latency_ms=p99,
                mean_latency_ms=mean,
                request_count=latency_count,
                effective_remaining=effective_remaining,
                limit=limit,
                safety_buffer=safety_buffer,
                burn_rate_urgency=burn_urgency,
                expiry_urgency=float(cand_meta.get("expiry_urgency", 0.0) or 0.0),
                scarcity=float(cand_meta.get("scarcity", 0.0) or 0.0),
                retry_expected_cost=float(cand_meta.get("retry_expected_cost_per_request", 0.0) or 0.0),
                uncertainty=float(cand_meta.get("uncertainty_score", 0.0) or 0.0),
                capability_match=True,
            )
        return metrics_by_key

    def _instantiate_driver(self, driver_cls, candidate: Candidate):
        """Instantiate a driver class using the arguments its contract requires.

        Drivers that delegate the full HTTP exchange (see
        ``ProviderDriver.delegates_request_execution``) need connection
        configuration. Non-delegating drivers remain compatible with the
        legacy direct-client path and are constructed without arguments.
        """
        if getattr(driver_cls, "delegates_request_execution", False):
            return driver_cls(base_url=self._upstream_config(candidate.upstream)["base_url"])
        return driver_cls()

    def _get_driver_registry(self):
        """Lazily load and return the default driver registry."""
        if getattr(self, "driver_registry", None) is None:
            from apps.gateway.providers.registry import default_driver_registry
            self.driver_registry = default_driver_registry()
        return self.driver_registry

    def _resolve_driver(self, candidate):
        """Resolve the ProviderDriver class for a candidate upstream.

        Resolution order:
          1. Explicit driver_id in candidate metadata
          2. driver_id configured in the upstream's config dictionary
          3. Keyword heuristics on the upstream string ID matched against
             the default driver registry aliases
        Returns the Driver class or None if no driver could be resolved.
        """
        registry = self._get_driver_registry()

        # 1. Check candidate metadata first
        driver_id = candidate.metadata.get("driver_id")
        if driver_id:
            try:
                return registry.resolve(driver_id)
            except Exception:
                pass

        # 2. Check upstream config section
        upstream_cfg = self.config.get("upstreams", {}).get(candidate.upstream, {})
        if isinstance(upstream_cfg, dict):
            driver_id = upstream_cfg.get("driver_id")
            if driver_id:
                try:
                    return registry.resolve(driver_id)
                except Exception:
                    pass

        # 3. Heuristic fallback: keyword matching on upstream ID
        upstream_lower = candidate.upstream.lower().replace("-", "").replace("_", "")
        heuristic_map = {
            "anthropic": "anthropic-compatible",
            "openai": "openai-compatible",
            "gemini": "gemini-compatible",
        }
        for keyword, alias in heuristic_map.items():
            if keyword in upstream_lower:
                try:
                    return registry.resolve(alias)
                except Exception:
                    break  # No partial match on other keywords if this one fails
        return None

    def _active_usage_ledger(self):
        """Return per-request DB ledger when set, else the injected fallback ledger."""
        return _REQUEST_USAGE_LEDGER.get() or self._usage_ledger

    def _with_usage_cost(self, candidate: Candidate, usage: dict[str, Any]) -> dict[str, Any]:
        """Attach estimated USD cost when catalog prices cover observed token usage."""
        if usage.get("actual_cost") is not None:
            return usage

        prices = self.catalog.get("prices", {}) if isinstance(self.catalog, dict) else {}
        price_info = prices.get(candidate.model) or prices.get(candidate.model.lower()) or {}
        input_price = price_info.get("input_per_million")
        output_price = price_info.get("output_per_million")
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)

        if (input_tokens > 0 and input_price is None) or (output_tokens > 0 and output_price is None):
            return usage

        cost = (input_tokens * float(input_price or 0) + output_tokens * float(output_price or 0)) / 1_000_000
        enriched = dict(usage)
        enriched["actual_cost"] = cost
        enriched.setdefault("currency", price_info.get("currency") or "USD")
        return enriched

    async def _response_json_or_none(self, response: Any) -> dict[str, Any] | None:
        try:
            response_json = response.json()
            if inspect.isawaitable(response_json):
                response_json = await response_json
            if isinstance(response_json, dict):
                return response_json
        except Exception:
            pass

        content = getattr(response, "body", None)
        if not isinstance(content, (bytes, bytearray)):
            content = getattr(response, "content", None)
        if not isinstance(content, (bytes, bytearray)):
            return None
        try:
            parsed = json.loads(bytes(content).decode("utf-8"))
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def _classify_provider_response(self, candidate: Candidate, response: Any) -> dict[str, Any]:
        preset = getattr(response, "_smart_router_classification", None)
        if isinstance(preset, dict):
            return preset
        body = await self._response_json_or_none(response)
        status_code = getattr(response, "status_code", None)
        headers = getattr(response, "headers", None)
        driver_cls = self._resolve_driver(candidate)
        if driver_cls is not None:
            try:
                driver_instance = self._instantiate_driver(driver_cls, candidate)
                return driver_instance.classify_error(status_code=status_code, body=body, headers=headers)
            except Exception:
                self.logger.debug("driver error classifier failed for candidate %s", candidate.key, exc_info=True)
        from apps.gateway.providers.error_classifier import classify_provider_error

        return classify_provider_error(status_code=status_code, body=body, headers=headers)

    async def _record_usage_event(
        self,
        *,
        request_id: str,
        attempt_id: str,
        candidate: Candidate,
        response_json: dict[str, Any],
    ) -> None:
        """Parse token usage statistics from an upstream response and record them into the usage ledger.

        This method is safe to call even when:
        - No driver can be resolved for the candidate (logged as debug)
        - The response body does not contain usage metadata (silently skipped)
        - The ledger raises an exception (caught and logged, never breaks data-plane)
        """
        ledger = self._active_usage_ledger()
        if ledger is None:
            return

        # Resolve driver lazily; skip if none found
        driver_cls = self._resolve_driver(candidate)
        if driver_cls is None:
            self.logger.debug(
                "no driver resolved for candidate %s, skipping usage parse",
                candidate.key,
            )
            return

        # Parse usage tokens from upstream JSON
        parsed: dict[str, Any] | None = None
        try:
            driver_instance = self._instantiate_driver(driver_cls, candidate)
            parsed = driver_instance.parse_usage(response_json)
        except Exception:
            self.logger.warning(
                "failed to parse usage payload using driver for candidate %s",
                candidate.key,
                exc_info=True,
            )

        # If parse_usage returned empty/None (e.g. provider omitted usage field)
        if not parsed:
            return

        # Mitigate zero-fabrication bug: if the driver parses 0 tokens, do not record a fabricated event.
        if parsed.get("input_tokens") == 0 and parsed.get("output_tokens") == 0:
            return

        parsed = self._with_usage_cost(candidate, parsed)

        # Build UsageEvent and persist to ledger
        try:
            from apps.gateway.usage.ledger import UsageEvent

            credential_id = candidate.metadata.get("credential_id")

            event = UsageEvent.from_parsed_usage(
                request_id=request_id,
                attempt_id=attempt_id,
                provider_connection_id=candidate.upstream,
                credential_id=credential_id,
                model_resource_id=candidate.model,
                usage=parsed,
            )

            result = ledger.record_usage(event)
            if inspect.isawaitable(result):
                await result
            request_events = _REQUEST_USAGE_EVENTS.get()
            if request_events is not None:
                request_events.append(event)
        except Exception:
            self.logger.warning(
                "failed to create or record UsageEvent for candidate %s",
                candidate.key,
                exc_info=True,
            )

    async def _parse_and_record_stream_usage(
        self,
        *,
        request_id: str,
        attempt_id: str,
        candidate: Candidate,
        sse_buffer: bytearray | None = None,
    ) -> None:
        """Best-effort: extract usage metadata from accumulated SSE text and record a UsageEvent.

        Anthropic SSE streams include ``usage`` inside multiple events:
        ``message_start`` (input tokens, nested under ``message.usage``) and
        ``message_delta`` (output tokens, at top level).

        OpenAI streams embed usage as top-level ``usage`` on the final chunk.

        This collector parses every ``data:`` line from the buffer, collects
        all valid JSON objects, and delegates to each driver's ``parse_usage``
        which returns an empty dict when no usage is found.  Consolidation
        sums across all payloads; the zero-fabrication guard then discards
        cases where everything parsed to zeros.
        """
        if self._active_usage_ledger() is None:
            return
        if not sse_buffer:
            return

        # Decode accumulated SSE bytes → text
        text = sse_buffer.decode("utf-8", errors="replace")
        json_payloads: list[dict[str, Any]] = []

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data: "):
                payload_str = stripped[6:].strip()
            elif stripped == "data:":
                continue
            else:
                continue

            if not payload_str or payload_str == "[DONE]":
                continue

            try:
                obj = json.loads(payload_str)
            except (json.JSONDecodeError, ValueError):
                continue

            # Collect all valid JSON objects; let the driver decide whether
            # it can extract meaningful usage from each one.  Anthropic SSE
            # spreads input_tokens in ``message_start`` and output_tokens in
            # ``message_delta`` — filtering on top-level "usage" alone drops
            # the former entirely.
            if isinstance(obj, dict):
                json_payloads.append(obj)

        # Resolve driver lazily; skip if none found
        driver_cls = self._resolve_driver(candidate)
        if driver_cls is None:
            return

        # Consolidate usage tokens across all SSE payloads (e.g. input_tokens in message_start,
        # output_tokens in message_delta for Anthropic streams).
        consolidated_input = 0
        consolidated_output = 0
        has_parsed_any = False

        try:
            driver_instance = driver_cls()
            for payload in json_payloads:
                parsed = driver_instance.parse_usage(payload)
                if parsed:
                    consolidated_input += parsed.get("input_tokens", 0)
                    consolidated_output += parsed.get("output_tokens", 0)
                    has_parsed_any = True
        except Exception:
            self.logger.warning(
                "failed to parse streaming usage payload for candidate %s",
                candidate.key,
                exc_info=True,
            )

        if not has_parsed_any or (consolidated_input == 0 and consolidated_output == 0):
            return

        consolidated_payload = {
            "usage": {
                "input_tokens": consolidated_input,
                "output_tokens": consolidated_output,
            }
        }
        # If the driver is OpenAI, format payload as OpenAI usage fields
        if getattr(driver_cls, "driver_id", "") == "generic-openai" or "openai" in candidate.upstream.lower():
            consolidated_payload = {
                "usage": {
                    "prompt_tokens": consolidated_input,
                    "completion_tokens": consolidated_output,
                }
            }

        await self._record_usage_event(
            request_id=request_id,
            attempt_id=attempt_id,
            candidate=candidate,
            response_json=consolidated_payload,
        )

    def _get_latest_usage_tokens(self, *, attempt_id: str) -> dict[str, int]:
        """Return {input_tokens, output_tokens, total_tokens} from the latest UsageEvent for ``attempt_id``.

        Returns zeros when no event was recorded or the ledger is unavailable.
        """
        request_events = _REQUEST_USAGE_EVENTS.get()
        if request_events is not None:
            events = request_events
        else:
            ledger = self._active_usage_ledger()
            if ledger is None:
                return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            events = getattr(ledger, "_events", [])
        for evt in reversed(events):
            if getattr(evt, "attempt_id", None) == attempt_id:
                return {
                    "input_tokens": getattr(evt, "input_tokens", 0),
                    "output_tokens": getattr(evt, "output_tokens", 0),
                    "total_tokens": getattr(evt, "total_tokens", 0),
                }
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    async def _record_usage_request(
        self,
        *,
        request_id: str,
        route_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ledger = self._active_usage_ledger()
        if ledger is None:
            return
        record = RequestRecord(
            request_id=request_id,
            route_id=route_name,
            logical_model=route_name,
            metadata=metadata or {},
        )
        try:
            method = ledger.record_request
            params = inspect.signature(method).parameters
            if "request_id" in params:
                result = method(
                    request_id=request_id,
                    route_id=route_name,
                    logical_model=route_name,
                    metadata=metadata,
                )
            else:
                result = method(record)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Ledger failures must not break data-plane traffic.
            pass

    async def _record_usage_attempt(
        self,
        *,
        request_id: str | None,
        attempt_id: str,
        candidate: Candidate,
        status: str,
    ) -> None:
        ledger = self._active_usage_ledger()
        if ledger is None or request_id is None:
            return
        record = AttemptRecord(
            request_id=request_id,
            attempt_id=attempt_id,
            provider_connection_id=candidate.upstream,
            model_resource_id=candidate.model,
            status=status,
        )
        try:
            method = ledger.record_attempt
            params = inspect.signature(method).parameters
            if "request_id" in params:
                result = method(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    provider_connection_id=candidate.upstream,
                    model_resource_id=candidate.model,
                    status=status,
                )
            else:
                result = method(record)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Ledger failures must not break data-plane traffic.
            pass

    async def handle_messages(self, body: dict[str, Any], incoming_headers: Any, path: str) -> Response:
        route_name = body.get("model")
        request_id = uuid.uuid4().hex
        if not isinstance(route_name, str) or route_name not in self.routes:
            await self._record_usage_request(
                request_id=request_id,
                route_name=route_name if isinstance(route_name, str) else "<missing>",
                metadata={"status": "rejected", "reason": "unknown_model", "path": path},
            )
            return JSONResponse(
                status_code=404,
                content={"error": {"type": "unknown_model", "message": "unknown logical router model"}},
            )

        reservation_id = None
        resource_id = None
        # Quota admission check
        if self.quota_reservations is not None:
            from apps.gateway.quota.reservations import QuotaReservationRequest
            resource_id = f"model:{route_name}"
            try:
                reservation_id = uuid.uuid4().hex
                result = await self.quota_reservations.reserve_many(
                    reservation_id=reservation_id,
                    requests=[QuotaReservationRequest(resource_id, amount=1)],
                )
                if not result.accepted:
                    await self._record_usage_request(
                        request_id=request_id,
                        route_name=route_name,
                        metadata={"status": "rejected", "reason": "quota_exhausted", "path": path},
                    )
                    return JSONResponse(
                        status_code=503,
                        content={"error": {"type": "quota_exhausted", "message": "quota exhausted"}},
                    )
            except KeyError:
                # No quota resource defined for this route; allow the request
                reservation_id = None
                resource_id = None

        # Session hint: chỉ lấy ID để scoring affinity — không đụng payload/secret
        conversation_thread = self._conversation_thread_hint(body, incoming_headers)
        required_capabilities = self._required_capabilities_from_body(body)

        candidates = await self._candidate_order(
            route_name,
            conversation_thread=conversation_thread,
            required_capabilities=required_capabilities,
        )
        if not candidates:
            # If we had a reservation, release it before returning overloaded
            if reservation_id is not None and resource_id is not None:
                await self.quota_reservations.release(reservation_id)
            await self._record_usage_request(
                request_id=request_id,
                route_name=route_name,
                metadata={"status": "rejected", "reason": "overloaded", "path": path},
            )
            return JSONResponse(
                status_code=503,
                content={"error": {"type": "overloaded", "message": "all candidates are cooling down or unavailable"}},
            )

        # Retry budget theo policy preset — giữ state trong một request, không lưu payload.
        from apps.gateway.routing.retry import RetryBudget
        try:
            retry_policy = self._scoring_config.effective_preset_for_route(route_name).retry
        except Exception:
            from apps.gateway.routing.presets import RetryPolicy
            retry_policy = RetryPolicy()
        estimated_input_tokens = self._estimate_input_tokens(body)
        retry_budget = RetryBudget(
            retry_policy,
            estimated_input_tokens=estimated_input_tokens,
        )

        await self._record_usage_request(request_id=request_id, route_name=route_name)

        if bool(body.get("stream", False)) and path.endswith("/messages"):
            return await self._stream_messages(body, incoming_headers, candidates, route_name, path, reservation_id, resource_id, request_id, conversation_thread, retry_budget=retry_budget)
        return await self._non_stream_messages(body, incoming_headers, candidates, route_name, path, reservation_id, resource_id, request_id, conversation_thread, retry_budget=retry_budget)

    async def _non_stream_messages(
        self,
        body: dict[str, Any],
        incoming_headers: Any,
        candidates: list[Candidate],
        route_name: str,
        path: str,
        reservation_id: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        conversation_thread: str | None = None,
        retry_budget: Any | None = None,
    ) -> Response:
        last_failure: str | None = None
        for index, candidate in enumerate(candidates):
            has_next = index < len(candidates) - 1
            attempt_id = uuid.uuid4().hex
            request_body = dict(body)
            request_body["model"] = candidate.model
            _start = time.monotonic()
            # Resolve driver capability without provider-specific branching.
            driver_cls = self._resolve_driver(candidate)
            delegates_execution = bool(getattr(driver_cls, "delegates_request_execution", False))
            if driver_cls is None or not delegates_execution:
                # Fallback: use direct HTTP client (old behavior)
                try:
                    headers = self._upstream_headers(candidate.upstream, incoming_headers)
                    client = self.clients[candidate.upstream]
                    response = await client.post(self._url(candidate.upstream, path), headers=headers, json=request_body)
                except (httpx.RequestError, RouterConfigurationError) as exc:
                    elapsed_ms = (time.monotonic() - _start) * 1000
                    if self._score_calculator:
                        self._latency_tracker.record(candidate.key, elapsed_ms)
                    last_failure = _safe_error(exc)
                    classification = {
                        "kind": "TRANSIENT_NETWORK",
                        "retryable": True,
                        "scope": "connection",
                        "retry_after": None,
                        "reset_at": None,
                        "consumption_uncertainty": "none",
                        "status_code": None,
                    }
                    decision = self._failure_runtime_decision(None, classification, has_next=has_next, retry_budget=retry_budget, extra_input_tokens=self._estimate_input_tokens(body), extra_latency_ms=int(elapsed_ms))
                    await self._record_failure(candidate, None, last_failure, classification, decision)
                    await self._record_usage_attempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        candidate=candidate,
                        status="TRANSIENT_NETWORK",
                    )
                    if decision.try_next:
                        if retry_budget is not None:
                            try:
                                retry_budget.record_retry(extra_input_tokens=self._estimate_input_tokens(body), extra_latency_ms=int(elapsed_ms))
                            except Exception:
                                pass
                        continue
                    break
                if response.status_code >= 400:
                    classification = await self._classify_provider_response(candidate, response)
                    attempt_status = str(classification.get("kind") or "UNKNOWN")
                    quota_observed = False
                    if attempt_status == "QUOTA_EXHAUSTED":
                        quota_observed = await self._apply_quota_exhaustion_observation(candidate, classification)
                    decision = self._failure_runtime_decision(
                        response.status_code,
                        classification,
                        has_next=has_next,
                        quota_observed=quota_observed,
                        retry_budget=retry_budget,
                        extra_input_tokens=self._estimate_input_tokens(body),
                        extra_latency_ms=int((time.monotonic() - _start) * 1000),
                    )
                    last_failure = f"upstream returned {response.status_code} ({attempt_status})"
                    await self._record_failure(candidate, response.status_code, last_failure, classification, decision)
                    await self._record_usage_attempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        candidate=candidate,
                        status=attempt_status,
                    )
                    if decision.try_next:
                        if retry_budget is not None:
                            try:
                                retry_budget.record_retry(extra_input_tokens=self._estimate_input_tokens(body), extra_latency_ms=int((time.monotonic() - _start) * 1000))
                            except Exception:
                                pass
                        continue
                    await self._finalize_error_reservation(reservation_id, resource_id, classification, decision)
                    return Response(
                        content=response.content,
                        status_code=response.status_code,
                        headers=_response_headers(response.headers),
                    )
                # Success
                await self._record_usage_attempt(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    candidate=candidate,
                    status="success",
                )
                input_tokens = output_tokens = total_tokens = 0
                if path != "/v1/messages/count_tokens":
                    response_json = await self._response_json_or_none(response)
                    if response_json is not None:
                        await self._record_usage_event(
                            request_id=request_id,
                            attempt_id=attempt_id,
                            candidate=candidate,
                            response_json=response_json,
                        )
                if reservation_id is not None and resource_id is not None:
                    tokens = self._get_latest_usage_tokens(attempt_id=attempt_id)
                    input_tokens = tokens["input_tokens"]
                    output_tokens = tokens["output_tokens"]
                    total_tokens = tokens["total_tokens"]
                    if total_tokens > 0:
                        await self.quota_reservations.reconcile(reservation_id, {resource_id: total_tokens})
                    else:
                        await self.quota_reservations.reconcile(reservation_id, {resource_id: 1})
                elapsed_ms = (time.monotonic() - _start) * 1000
                if self._score_calculator:
                    self._latency_tracker.record(candidate.key, elapsed_ms)
                self._remember_session_affinity(conversation_thread, candidate.key)
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=_response_headers(response.headers),
                )
            # Use driver
            driver = driver_cls(
                base_url=self._upstream_config(candidate.upstream)["base_url"],
                endpoint=path,
                timeout=self._timeout(),
                headers=self._upstream_headers(candidate.upstream, incoming_headers),
                client=self.clients[candidate.upstream],
            )
            # Build request object for driver
            req = types.SimpleNamespace(
                method="POST",
                headers=incoming_headers,  # driver will filter and add auth headers
                body=None,
                json=request_body,
            )
            try:
                result = await driver.execute(None, req)
            except Exception as exc:
                elapsed_ms = (time.monotonic() - _start) * 1000
                if self._score_calculator:
                    self._latency_tracker.record(candidate.key, elapsed_ms)
                last_failure = _safe_error(exc)
                classification = {
                    "kind": "TRANSIENT_NETWORK",
                    "retryable": True,
                    "scope": "connection",
                    "retry_after": None,
                    "reset_at": None,
                    "consumption_uncertainty": "none",
                    "status_code": None,
                }
                decision = self._failure_runtime_decision(
                    None, classification, has_next=has_next,
                    retry_budget=retry_budget,
                    extra_input_tokens=self._estimate_input_tokens(body),
                    extra_latency_ms=int(elapsed_ms),
                )
                await self._record_failure(candidate, None, last_failure, classification, decision)
                await self._record_usage_attempt(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    candidate=candidate,
                    status="TRANSIENT_NETWORK",
                )
                if decision.try_next:
                    if retry_budget is not None:
                        try:
                            retry_budget.record_retry(extra_input_tokens=self._estimate_input_tokens(body), extra_latency_ms=int(elapsed_ms))
                        except Exception:
                            pass
                    continue
                break
            status_code = result["status_code"]
            classification = result.get("classification", {})
            attempt_status = str(classification.get("kind") or "UNKNOWN")
            if status_code >= 400:
                quota_observed = False
                if attempt_status == "QUOTA_EXHAUSTED":
                    quota_observed = await self._apply_quota_exhaustion_observation(candidate, classification)
                decision = self._failure_runtime_decision(
                    status_code,
                    classification,
                    has_next=has_next,
                    quota_observed=quota_observed,
                    retry_budget=retry_budget,
                    extra_input_tokens=self._estimate_input_tokens(body),
                    extra_latency_ms=int((time.monotonic() - _start) * 1000),
                )
                last_failure = f"upstream returned {status_code} ({attempt_status})"
                await self._record_failure(candidate, status_code, last_failure, classification, decision)
                await self._record_usage_attempt(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    candidate=candidate,
                    status=attempt_status,
                )
                if decision.try_next:
                    if retry_budget is not None:
                        try:
                            retry_budget.record_retry(
                                extra_input_tokens=self._estimate_input_tokens(body),
                                extra_latency_ms=int((time.monotonic() - _start) * 1000),
                            )
                        except Exception:
                            pass
                    continue
                await self._finalize_error_reservation(reservation_id, resource_id, classification, decision)
                # Convert body string to bytes for Response
                body_bytes = result["body"].encode("utf-8") if isinstance(result["body"], str) else result["body"]
                return Response(
                    content=body_bytes,
                    status_code=status_code,
                    headers=_response_headers(result.get("headers", {})),
                )
            # Success
            await self._record_usage_attempt(
                request_id=request_id,
                attempt_id=attempt_id,
                candidate=candidate,
                status="success",
            )
            await self._record_success(candidate, status_code)
            # Record usage from driver's parsed usage
            usage = result.get("usage", {})
            if usage and path != "/v1/messages/count_tokens":
                # Construct a response_json-like dict for _record_usage_event
                # It expects a dict with usage field
                response_json = {"usage": usage}
                await self._record_usage_event(
                    request_id=request_id,
                    attempt_id=attempt_id,
                    candidate=candidate,
                    response_json=response_json,
                )
            if reservation_id is not None and resource_id is not None:
                tokens = self._get_latest_usage_tokens(attempt_id=attempt_id)
                input_tokens = tokens["input_tokens"]
                output_tokens = tokens["output_tokens"]
                total_tokens = tokens["total_tokens"]
                if total_tokens > 0:
                    await self.quota_reservations.reconcile(reservation_id, {resource_id: total_tokens})
                else:
                    await self.quota_reservations.reconcile(reservation_id, {resource_id: 1})
            elapsed_ms = (time.monotonic() - _start) * 1000
            if self._score_calculator:
                self._latency_tracker.record(candidate.key, elapsed_ms)
            self._remember_session_affinity(conversation_thread, candidate.key)
            # Convert body to bytes
            body_bytes = result["body"].encode("utf-8") if isinstance(result["body"], str) else result["body"]
            return Response(
                content=body_bytes,
                status_code=status_code,
                headers=_response_headers(result.get("headers", {})),
            )
        # All candidates failed. Release reservation if held.
        if reservation_id is not None and resource_id is not None:
            await self.quota_reservations.release(reservation_id)
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
        reservation_id: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        conversation_thread: str | None = None,
        retry_budget: Any | None = None,
    ) -> Response:
        last_failure: str | None = None
        for index, candidate in enumerate(candidates):
            has_next = index < len(candidates) - 1
            attempt_id = uuid.uuid4().hex
            request_body = dict(body)
            request_body["model"] = candidate.model
            _stream_start = time.monotonic()
            opened = await self._open_stream(candidate, request_body, incoming_headers, path)
            if isinstance(opened, Response):
                _elapsed_ms = (time.monotonic() - _stream_start) * 1000
                if self._score_calculator:
                    self._latency_tracker.record(candidate.key, _elapsed_ms)
                if opened.status_code >= 400:
                    classification = await self._classify_provider_response(candidate, opened)
                    attempt_status = str(classification.get("kind") or "UNKNOWN")
                    quota_observed = False
                    if attempt_status == "QUOTA_EXHAUSTED":
                        quota_observed = await self._apply_quota_exhaustion_observation(candidate, classification)
                    decision = self._failure_runtime_decision(
                        opened.status_code,
                        classification,
                        has_next=has_next,
                        quota_observed=quota_observed,
                        retry_budget=retry_budget,
                        extra_input_tokens=self._estimate_input_tokens(body),
                        extra_latency_ms=int(_elapsed_ms),
                    )
                    last_failure = f"upstream returned {opened.status_code} ({attempt_status})"
                    await self._record_failure(candidate, opened.status_code, last_failure, classification, decision)
                    await self._record_usage_attempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        candidate=candidate,
                        status=attempt_status,
                    )
                    if decision.try_next:
                        if retry_budget is not None:
                            try:
                                retry_budget.record_retry(extra_input_tokens=self._estimate_input_tokens(body), extra_latency_ms=int(_elapsed_ms))
                            except Exception:
                                pass
                        continue
                    # Non-retryable error Response (e.g. invalid request);
                    # no stream was opened, so we cannot know actual token usage.
                    await self._finalize_error_reservation(reservation_id, resource_id, classification, decision)
                    return opened
            stream = opened
            response_headers = _response_headers(stream.response.headers)
            # Capture candidate info for closure
            _candidate = candidate
            # Accumulate raw SSE bytes so we can parse usage at end-of-stream
            _sse_buf: bytearray = bytearray()

            async def iterator() -> AsyncIterator[bytes]:
                try:
                    if stream.first_chunk:
                        yield stream.first_chunk
                        _sse_buf.extend(stream.first_chunk)
                    async for chunk in stream.iterator:
                        _sse_buf.extend(chunk)
                        yield chunk
                    # Stream completed successfully — record attempt first,
                    # parse usage, then reconcile with actual token counts.
                    await self._record_success(_candidate, stream.response.status_code)
                    _elapsed_ms = (time.monotonic() - _stream_start) * 1000
                    if self._score_calculator:
                        self._latency_tracker.record(_candidate.key, _elapsed_ms)
                    await self._record_usage_attempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        candidate=_candidate,
                        status="success",
                    )
                    await self._parse_and_record_stream_usage(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        candidate=_candidate,
                        sse_buffer=_sse_buf,
                    )
                    self._remember_session_affinity(conversation_thread, _candidate.key)
                    if reservation_id is not None and resource_id is not None:
                        tokens = self._get_latest_usage_tokens(attempt_id=attempt_id)
                        total_tokens = tokens["total_tokens"]
                        if total_tokens > 0:
                            await self.quota_reservations.reconcile(reservation_id, {resource_id: total_tokens})
                        else:
                            # No usage parsed — fall back to 1 request as before
                            await self.quota_reservations.reconcile(reservation_id, {resource_id: 1})
                except Exception as exc:
                    # Stream failed; record the failed attempt first
                    # so the ledger has a valid reference for best-effort
                    # usage parsing below.
                    _elapsed_ms = (time.monotonic() - _stream_start) * 1000
                    if self._score_calculator:
                        self._latency_tracker.record(_candidate.key, _elapsed_ms)
                    if reservation_id is not None and resource_id is not None:
                        await self.quota_reservations.release(reservation_id)
                    await self._record_failure(_candidate, None, _safe_error(exc))
                    await self._record_usage_attempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        candidate=_candidate,
                        status="failed",
                    )
                    # Best-effort usage parse: attempt to extract usage
                    # from any buffered SSE data before raising so
                    # failover candidates still get accurate token accounting.
                    try:
                        await self._parse_and_record_stream_usage(
                            request_id=request_id,
                            attempt_id=attempt_id,
                            candidate=_candidate,
                            sse_buffer=_sse_buf,
                        )
                    except Exception:
                        pass  # Never let usage parse break failover
                    raise
                finally:
                    await stream.context_manager.__aexit__(None, None, None)

            return StreamingResponse(iterator(), status_code=stream.response.status_code, headers=response_headers)
        # All candidates failed. Release reservation if held.
        if reservation_id is not None and resource_id is not None:
            await self.quota_reservations.release(reservation_id)
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
        except (httpx.RequestError, RouterConfigurationError):
            response = JSONResponse(status_code=503, content={"error": {"type": "upstream_unavailable", "message": "connection failed"}})
            response._smart_router_classification = {
                "kind": "TRANSIENT_NETWORK",
                "retryable": True,
                "scope": "connection",
                "retry_after": None,
                "reset_at": None,
                "consumption_uncertainty": "none",
                "status_code": None,
            }
            return response

        if response.status_code >= 400:
            content = await response.aread()
            await context_manager.__aexit__(None, None, None)
            return Response(content=content, status_code=response.status_code, headers=_response_headers(response.headers))

        iterator = response.aiter_bytes()
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            first = b""
        except httpx.RequestError:
            await context_manager.__aexit__(None, None, None)
            response = JSONResponse(status_code=503, content={"error": {"type": "upstream_unavailable", "message": "stream failed before output"}})
            response._smart_router_classification = {
                "kind": "TRANSIENT_NETWORK",
                "retryable": True,
                "scope": "connection",
                "retry_after": None,
                "reset_at": None,
                "consumption_uncertainty": "none",
                "status_code": None,
            }
            return response
        return OpenStream(context_manager, response, iterator, first, candidate)

    def _classification_kind(self, classification: dict[str, Any] | None, status: int | None) -> str:
        if classification and classification.get("kind"):
            return str(classification["kind"])
        if status in FAILOVER_STATUSES or (status is not None and status >= 500):
            return "TRANSIENT_NETWORK"
        return "UNKNOWN"

    def _parse_retry_delay_seconds(self, retry_after: Any, reset_at: Any, *, max_seconds: float = 86_400.0) -> float | None:
        def _bounded(value: float | None) -> float | None:
            if value is None or value < 0:
                return None
            return min(value, max_seconds)

        if retry_after:
            retry_text = str(retry_after).strip()
            try:
                return _bounded(float(retry_text))
            except ValueError:
                try:
                    dt = parsedate_to_datetime(retry_text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return _bounded((dt - datetime.now(timezone.utc)).total_seconds())
                except Exception:
                    pass

        if reset_at:
            reset_text = str(reset_at).strip()
            try:
                numeric = float(reset_text)
                if numeric > 1_000_000_000:
                    return _bounded(numeric - datetime.now(timezone.utc).timestamp())
                return _bounded(numeric)
            except ValueError:
                try:
                    iso_text = reset_text.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(iso_text)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return _bounded((dt - datetime.now(timezone.utc)).total_seconds())
                except Exception:
                    return None
        return None

    def _default_cooldown_seconds(self, kind: str, consecutive_failures: int) -> float | None:
        attempts = max(0, consecutive_failures - 1)
        if kind == "RATE_LIMIT":
            return min(120.0, 15.0 * (2 ** attempts))
        if kind in {"OVERLOADED", "TRANSIENT_NETWORK"}:
            return min(120.0, 15.0 * (2 ** attempts))
        if kind in {"QUOTA_EXHAUSTED", "AUTH_EXPIRED", "AUTH_REVOKED", "MODEL_NOT_FOUND"}:
            return 3_600.0
        if kind == "UNKNOWN":
            return min(120.0, 15.0 * (2 ** attempts))
        return None

    def _failure_runtime_decision(
        self,
        status: int | None,
        classification: dict[str, Any] | None,
        *,
        has_next: bool,
        consecutive_failures: int = 1,
        quota_observed: bool = False,
        retry_budget: Any | None = None,
        extra_input_tokens: int = 0,
        extra_latency_ms: int = 0,
    ) -> FailureRuntimeDecision:
        kind = self._classification_kind(classification, status)
        scope = str(classification.get("scope")) if classification and classification.get("scope") else None
        retry_after = classification.get("retry_after") if classification else None
        reset_at = classification.get("reset_at") if classification else None
        parsed_delay = self._parse_retry_delay_seconds(retry_after, reset_at)

        request_scoped = {"INVALID_REQUEST", "CONTENT_POLICY", "CONTEXT_TOO_LARGE"}
        suppression = {"QUOTA_EXHAUSTED", "AUTH_EXPIRED", "AUTH_REVOKED", "MODEL_NOT_FOUND"}
        transient = {"RATE_LIMIT", "OVERLOADED", "TRANSIENT_NETWORK"}

        # Retry budget enforcement (M5 §21): chặn retry khi hết ngân sách attempts / tokens / latency
        budget_allows = True
        if retry_budget is not None and hasattr(retry_budget, "can_retry"):
            try:
                budget_allows = bool(
                    retry_budget.can_retry(
                        kind,
                        extra_input_tokens=extra_input_tokens,
                        extra_latency_ms=extra_latency_ms,
                    )
                )
            except Exception:
                budget_allows = True

        if kind in request_scoped:
            return FailureRuntimeDecision(kind, scope, False, False, None, False, False, "release")

        if kind == "QUOTA_EXHAUSTED":
            cooldown = None if quota_observed else (parsed_delay or self._default_cooldown_seconds(kind, consecutive_failures))
            return FailureRuntimeDecision(
                kind, scope, True, False, cooldown,
                bool(has_next and budget_allows),
                not quota_observed, "reconcile_minimal",
            )

        if kind in {"AUTH_EXPIRED", "AUTH_REVOKED", "MODEL_NOT_FOUND"}:
            return FailureRuntimeDecision(
                kind,
                scope,
                True,
                False,
                parsed_delay or self._default_cooldown_seconds(kind, consecutive_failures),
                bool(has_next and budget_allows),
                False,
                "reconcile_minimal",
            )

        if kind in transient:
            return FailureRuntimeDecision(
                kind,
                scope,
                True,
                True,
                parsed_delay or self._default_cooldown_seconds(kind, consecutive_failures),
                bool(has_next and budget_allows),
                False,
                "reconcile_minimal",
            )

        compatible_transient = status in FAILOVER_STATUSES or (status is not None and status >= 500)
        return FailureRuntimeDecision(
            kind,
            scope,
            compatible_transient,
            compatible_transient,
            self._default_cooldown_seconds("UNKNOWN", consecutive_failures) if compatible_transient else None,
            bool(has_next and compatible_transient and budget_allows),
            False,
            "reconcile_minimal" if compatible_transient else "release",
        )

    async def _apply_quota_exhaustion_observation(self, candidate: Candidate, classification: dict[str, Any] | None) -> bool:
        if self.quota_reservations is None:
            return False
        try:
            from apps.gateway.quota.reservations import QuotaObservation
        except Exception:
            return False

        observed = False
        seen: set[str] = set()
        for resource_id in self._candidate_quota_resource_ids(candidate):
            if resource_id in seen:
                continue
            seen.add(resource_id)
            try:
                resource = await self.quota_reservations.snapshot(resource_id)
                if resource.metric != "requests":
                    continue
                observation = QuotaObservation(
                    resource_id=resource.resource_id,
                    limit=resource.limit,
                    used=resource.limit,
                    source="provider_error",
                    confidence="inferred",
                    safety_buffer=resource.safety_buffer,
                    hard_limit=True,
                )
                await self.quota_reservations.apply_observation(observation)
                observed = True
            except (KeyError, ValueError, TypeError):
                continue
            except Exception:
                self.logger.warning(
                    "quota observation skipped for upstream=%s model=%s",
                    candidate.upstream,
                    candidate.model,
                    exc_info=True,
                )
        return observed

    def _trip_router_engine_circuit(self, candidate: Candidate, cooldown_seconds: float | None) -> None:
        if self.router_engine is None or cooldown_seconds is None or cooldown_seconds <= 0:
            return
        try:
            from apps.gateway.routing.models import ResourceRef

            ref = ResourceRef(candidate.upstream, candidate.upstream, candidate.model)
            self.router_engine.circuit_repository.trip(ref, cooldown_seconds)
        except Exception:
            self.logger.debug("router engine circuit update skipped for candidate %s", candidate.key, exc_info=True)

    async def _finalize_error_reservation(
        self,
        reservation_id: str | None,
        resource_id: str | None,
        classification: dict[str, Any] | None,
        decision: FailureRuntimeDecision,
    ) -> None:
        if reservation_id is None or resource_id is None:
            return
        try:
            if decision.reservation_finalization == "release" or (classification or {}).get("consumption_uncertainty") == "none":
                await self.quota_reservations.release(reservation_id)
            else:
                await self.quota_reservations.reconcile(reservation_id, {resource_id: 1})
        except Exception:
            self.logger.warning("quota reservation finalization skipped", exc_info=True)

    async def _record_success(self, candidate: Candidate, status: int | None) -> None:
        async with self.state_lock:
            state = self.circuits.setdefault(candidate.key, CircuitState())
            state.cooldown_until = 0.0
            state.consecutive_failures = 0
            state.last_status = status
            state.last_error = None
            state.last_event = _now()
            state.last_kind = None
            state.last_scope = None
            state.retry_after = None
            state.reset_at = None
        self.logger.info("upstream=%s model=%s status=%s failover=false", candidate.upstream, candidate.model, status)
        # Record for smart scoring (non-blocking)
        if self._score_calculator is not None:
            self._score_calculator.record_success(candidate.key)

    async def _record_failure(
        self,
        candidate: Candidate,
        status: int | None,
        error: str,
        classification: dict[str, Any] | None = None,
        decision: FailureRuntimeDecision | None = None,
    ) -> None:
        if classification is None:
            from apps.gateway.providers.error_classifier import classify_provider_error

            classification = classify_provider_error(status_code=status)
        decision = decision or self._failure_runtime_decision(status, classification, has_next=True)
        if not decision.record_circuit:
            return

        async with self.state_lock:
            state = self.circuits.setdefault(candidate.key, CircuitState())
            if decision.record_scoring_failure:
                state.consecutive_failures += 1
            else:
                state.consecutive_failures = max(state.consecutive_failures, 1)
            state.last_status = status
            state.last_error = error[:240]
            state.last_event = _now()
            state.last_kind = decision.kind
            state.last_scope = decision.scope
            state.retry_after = str(classification.get("retry_after")) if classification.get("retry_after") is not None else None
            state.reset_at = str(classification.get("reset_at")) if classification.get("reset_at") is not None else None
            cooldown = decision.cooldown_seconds
            if cooldown is not None and cooldown > 0:
                state.cooldown_until = time.monotonic() + cooldown
            else:
                state.cooldown_until = max(state.cooldown_until, 0.0)
        self._trip_router_engine_circuit(candidate, cooldown)
        self.logger.warning(
            "upstream=%s model=%s status=%s kind=%s cooldown=%ss failover=%s",
            candidate.upstream,
            candidate.model,
            status,
            decision.kind,
            cooldown or 0,
            decision.try_next,
        )
        # Record for smart scoring (non-blocking)
        if decision.record_scoring_failure and self._score_calculator is not None:
            self._score_calculator.record_failure(candidate.key)

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
                        "last_kind": state.last_kind,
                        "last_scope": state.last_scope,
                        "retry_after": state.retry_after,
                        "reset_at": state.reset_at,
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


async def get_optional_usage_ledger_repo():
    """Yield a DB-backed usage ledger when explicitly enabled.

    Runtime DB persistence is opt-in so local/test deployments without a
    reachable DATABASE_URL keep the existing in-memory/no-ledger behavior.
    """
    if os.getenv("USAGE_LEDGER_DB_ENABLED", "false").lower() != "true":
        yield None
        return
    try:
        from apps.gateway.db.session import get_async_session_factory
        from apps.gateway.usage.ledger import UsageLedgerRepository

        factory = get_async_session_factory()
        session_cm = factory()
        session = await session_cm.__aenter__()
    except Exception:
        logging.getLogger("smart-router").warning(
            "usage ledger DB dependency unavailable; continuing without DB ledger",
            exc_info=True,
        )
        yield None
        return

    try:
        yield UsageLedgerRepository(session)
    finally:
        try:
            await session.commit()
        except Exception:
            logging.getLogger("smart-router").warning(
                "usage ledger commit failed", exc_info=True,
            )
        try:
            await session_cm.__aexit__(None, None, None)
        except Exception:
            logging.getLogger("smart-router").warning(
                "usage ledger session close failed", exc_info=True,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Router lifecycle ─────────────────────────────────────────────
    service = SmartRouter.from_environment()
    app.state.router = service
    await service.start()
    try:
        yield
    finally:
        await service.close()
    # ── Database engine lifecycle ─────────────────────────────────────
    dispose_engine()  # close pools; no-op if nothing was created


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
async def messages(
    request: Request,
    service: SmartRouter = Depends(get_authorized_service),
    usage_ledger: Any | None = Depends(get_optional_usage_ledger_repo),
) -> Response:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    ledger_token = _REQUEST_USAGE_LEDGER.set(usage_ledger)
    events_token = _REQUEST_USAGE_EVENTS.set([])
    try:
        return await service.handle_messages(body, request.headers, "/v1/messages")
    finally:
        _REQUEST_USAGE_EVENTS.reset(events_token)
        _REQUEST_USAGE_LEDGER.reset(ledger_token)


@app.post("/v1/messages/count_tokens")
async def count_tokens(
    request: Request,
    service: SmartRouter = Depends(get_authorized_service),
    usage_ledger: Any | None = Depends(get_optional_usage_ledger_repo),
) -> Response:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    ledger_token = _REQUEST_USAGE_LEDGER.set(usage_ledger)
    events_token = _REQUEST_USAGE_EVENTS.set([])
    try:
        return await service.handle_messages(body, request.headers, "/v1/messages/count_tokens")
    finally:
        _REQUEST_USAGE_EVENTS.reset(events_token)
        _REQUEST_USAGE_LEDGER.reset(ledger_token)


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
