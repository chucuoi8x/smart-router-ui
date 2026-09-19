"""Local Anthropic-compatible smart router with provider-agnostic core."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import math
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
from apps.gateway.quota.runtime_index import RuntimeQuotaIndex
from apps.gateway.quota.runtime_index_adapter import RuntimeQuotaIndexAdapter
from apps.gateway.routing.engine import RouterEngine
from apps.gateway.config.compiler import LegacyConfigCompiler
from apps.gateway.usage.ledger import AttemptRecord, RequestRecord
from apps.gateway.routing.scoring import (
    ScoringConfig, ScoringWeights, SmartScoreCalculator,
    RollingFailureRateTracker, LatencyTracker, SessionAffinityStore,
    CandidateMetrics,
)
from apps.gateway.workers.catalog_sync import (
    CatalogSyncError,
    CatalogSyncWorker,
    build_records,
    select_routes,
    state_from_records,
)
from apps.gateway.openai_compat import openai_request_to_router, router_response_to_openai, router_stream_to_openai

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
_STARTED_AT_MONO = time.monotonic()
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


@dataclass(frozen=True)
class Candidate:
    upstream: str
    model: str
    weight: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    # Auxiliary runtime provenance (connection:credential:model), excluded from
    # equality so legacy (upstream, model, weight, metadata) candidate identity
    # comparisons keep working while state keyed off the canonical resource.
    resource_key: str = field(default="", compare=False)

    @property
    def key(self) -> str:
        # Canonical schedulable identity (plan §3.3): connection:credential:model.
        # Prefer the resource key from the authoritative engine; otherwise derive
        # the credential from metadata so credential-scoped state (circuit,
        # latency, failure rate, affinity) never leaks to sibling credentials.
        if self.resource_key:
            return self.resource_key
        credential = self.metadata.get("credential_id") or self.metadata.get("credential_scope")
        if credential:
            return f"{self.upstream}:{credential}:{self.model}"
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
        self.logger = logging.getLogger("smart-router")
        self.catalog_worker = CatalogSyncWorker(
            config=config,
            clients=self.clients,
            base_dir=BASE_DIR,
            logger=self.logger,
            timeout_fn=self._timeout,
            upstream_config_fn=self._upstream_config,
            upstream_headers_fn=self._upstream_headers,
        )
        self.catalog: dict[str, Any] = self.catalog_worker.catalog
        self._configure_logging()
        # ── Smart scoring subsystem ──────────────────────────────────────
        scoring_data = config.get("smart_scheduler", {})
        self._scoring_config = ScoringConfig.from_dict(scoring_data) if scoring_data else ScoringConfig()
        self._failure_tracker = RollingFailureRateTracker(self._scoring_config.max_failure_history)
        self._latency_tracker = LatencyTracker(self._scoring_config.max_latency_history)
        self._session_store = SessionAffinityStore(self._scoring_config.session_affinity_ttl_seconds)
        self._score_calculator: SmartScoreCalculator | None = None
        self.quota_index = RuntimeQuotaIndex()
        # ── Single production RouterEngine ───────────────────────────────
        compiler = LegacyConfigCompiler()
        snapshot = compiler.compile_dict(config)
        engine_quota = quota_reservations
        if engine_quota is not None and not asyncio.iscoroutinefunction(
            getattr(engine_quota, "check_many", None),
        ):
            from apps.gateway.quota.adapter import AsyncQuotaFacade
            engine_quota = AsyncQuotaFacade(engine_quota)
        self.router_engine = RouterEngine(
            snapshot, quota_reservations=engine_quota, scoring_config=self._scoring_config,
            quota_index=self.quota_index,
        )
        # Bridge session store so remember_affinity() affects both calculators
        if self._score_calculator is not None and getattr(self.router_engine, "_score_calculator", None) is not None:
            eng_calc = self.router_engine._score_calculator  # type: ignore[union-attr]
            try:
                eng_calc._session_store = self._score_calculator._session_store  # type: ignore[attr-defined]
            except Exception:
                pass
        # Ensure our own quota_reservations is async-compatible.
        if (self.quota_reservations is not None
                and not asyncio.iscoroutinefunction(getattr(self.quota_reservations, "check_many", None))):
            from apps.gateway.quota.adapter import AsyncQuotaFacade
            self.quota_reservations = AsyncQuotaFacade(self.quota_reservations)  # type: ignore[assignment]
        # Publish resources already present in synchronous test/local backends.
        # Async/Redis backends are populated during startup hydration.
        initial_list = getattr(quota_reservations, "list_resources", None)
        if initial_list is not None and not inspect.iscoroutinefunction(initial_list):
            try:
                self.quota_index.replace_all(initial_list())
            except Exception:
                self.logger.debug("initial quota index publish skipped", exc_info=True)
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
        # Canonical credential identity (P0/PR-06) — runtime state must be
        # keyed per credential so sibling credentials stay isolated.
        for key in ("credential_id", "credential_scope"):
            val = item.get(key)
            if val:
                metadata[key] = str(val)
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
        # Reinitialize catalog worker with actual clients
        self.catalog_worker.clients = self.clients
        sync_config = self.catalog_worker._get_sync_config()
        if sync_config.get("enabled", False):
            await self.sync_catalog(initial=True)
            self.sync_task = asyncio.create_task(self._sync_loop(), name="catalog-sync")
        self.logger.info("router started on 127.0.0.1:8320")
        # Hydrate quota resources from the database so running routers have
        # fresh quota state on startup (covers Redis and InMemory backends).
        await self._hydrate_quota_from_db()
        if self.quota_reservations is not None and not self.quota_index.loaded:
            await RuntimeQuotaIndexAdapter(self.quota_index, self.quota_reservations).refresh_all()

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
            self.quota_index.replace_all(resources)
            self.logger.info("quota hydrated %d resource(s) from database", count)

    async def _sync_loop(self) -> None:
        interval = max(60, int(self.catalog_worker._get_sync_config().get("interval_seconds", 21600)))
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

    @staticmethod
    def _canonical_candidate_key(candidate: Candidate) -> str:
        """Canonical schedulable-resource key for runtime state.

        ``Candidate.key`` now carries connection:credential:model (from
        ``resource_key`` or ``metadata['credential_id']``), falling back to the
        legacy two-part key only for candidates with no credential dimension.
        """
        return candidate.key

    def _remember_session_affinity(self, conversation_thread: str | None, candidate: Candidate) -> None:
        if not conversation_thread:
            return
        candidate_key = self._canonical_candidate_key(candidate)
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
        """Delegate every routing strategy to RouterEngine."""
        resource_candidates = await self.router_engine.select_candidates_async(
            route_name,
            conversation_thread=conversation_thread,
            required_capabilities=required_capabilities,
        )
        return self._convert_resource_candidates(resource_candidates)

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
            group_key = resource.shared_group_id or resource.resource_id
            if group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            known.append(resource_id)
        return known

    def _quota_request_amount(
        self,
        resource: object,
        estimated_input_tokens: int,
        estimated_output_tokens: int = 0,
    ) -> int:
        metric = str(getattr(resource, "metric", "") or "").lower()
        if metric in {"tokens", "input_tokens", "tpm", "tokens_per_minute", "tpm_tokens"}:
            return max(1, int(estimated_input_tokens) + max(0, int(estimated_output_tokens)))
        return 1

    def _quota_risk_buffer(self, resource: object, amount: int, route_name: str | None = None) -> int:
        """Risk buffer theo ReservationPolicy.safety_buffer_ratio cho token metrics."""
        try:
            metric = str(getattr(resource, "metric", "") or "").lower()
            if metric in {"requests", "request", "concurrency"}:
                return 0
            ratio = 0.0
            try:
                preset = self._scoring_config.effective_preset_for_route(route_name)
                ratio = float(getattr(preset.reservation, "safety_buffer_ratio", 0.0) or 0.0)
            except Exception:
                ratio = 0.0
            if ratio <= 0:
                return 0
            return int(math.ceil(amount * ratio))
        except Exception:
            return 0

    async def _reconcile_reservation_usage(
        self,
        reservation_id: str | None,
        resource_id: str | None,
        *,
        total_tokens: int,
    ) -> None:
        """Reconcile every resource in batch reservation, metric-aware."""
        if reservation_id is None or resource_id is None or self.quota_reservations is None:
            return
        resource_ids = [rid for rid in resource_id.split(",") if rid]
        actual: dict[str, int] = {}
        for rid in resource_ids:
            try:
                resource = await self.quota_reservations.snapshot(rid)
                metric = str(getattr(resource, "metric", "") or "").lower()
                actual[rid] = total_tokens if metric in {"tokens", "input_tokens", "tpm", "tokens_per_minute", "tpm_tokens"} else 1
            except Exception:
                actual[rid] = total_tokens if len(resource_ids) == 1 else 1
        await self.quota_reservations.reconcile(reservation_id, actual)

    async def _reserve_for_candidate(
        self,
        candidate: Candidate,
        route_name: str,
        body: dict[str, Any],
        reservation_id: str,
    ) -> tuple[str | None, str | None, bool]:
        """Reserve quota for a single candidate; returns (reservation_id, resource_id, accepted)."""
        if self.quota_reservations is None:
            return (None, None, True)
        rids = await self._known_candidate_quota_resource_ids(candidate)
        if not rids:
            try:
                await self.quota_reservations.snapshot(f"model:{route_name}")
                rids = [f"model:{route_name}"]
            except KeyError:
                rids = []
        if not rids:
            return (None, None, True)
        est_in = self._estimate_input_tokens(body)
        try:
            est_out = int(body.get("max_tokens", 0) or 0)
        except Exception:
            est_out = 0
        reqs = []
        for rid in rids:
            try:
                res = await self.quota_reservations.snapshot(rid)
                amt = self._quota_request_amount(res, est_in, est_out)
                risk = self._quota_risk_buffer(res, amt, route_name)
            except Exception:
                amt = 1
                risk = 0
            from apps.gateway.quota.reservations import QuotaReservationRequest
            reqs.append(QuotaReservationRequest(rid, amount=amt, risk_buffer=risk))
        try:
            res = await self.quota_reservations.reserve_many(reservation_id=reservation_id, requests=reqs)
        except (KeyError, ValueError):
            return (None, None, True)
        if res.accepted:
            return (reservation_id, ",".join(rids), True)
        return (None, None, False)

    async def _maybe_transfer_reservation(
        self,
        current_reservation_id: str | None,
        current_resource_id: str | None,
        candidates: list,
        current_index: int,
        route_name: str,
        body: dict,
        request_id: str | None,
        classification: dict | None = None,
    ) -> tuple[str | None, str | None]:
        """Release current reservation and reserve for next eligible candidate (Step 107)."""
        if current_reservation_id is None or current_resource_id is None or self.quota_reservations is None:
            return (current_reservation_id, current_resource_id)
        if current_index + 1 >= len(candidates):
            return (current_reservation_id, current_resource_id)
        # If next candidate shares same quota resources, keep current reservation (no transfer)
        try:
            current_rids = set(rid for rid in (current_resource_id or "").split(",") if rid)
            nxt_candidate = candidates[current_index + 1] if current_index + 1 < len(candidates) else None
            if nxt_candidate is not None:
                nxt_rids = set(await self._known_candidate_quota_resource_ids(nxt_candidate))
                if not nxt_rids:
                    try:
                        await self.quota_reservations.snapshot(f"model:{nxt_candidate.model}")
                        nxt_rids = {f"model:{nxt_candidate.model}"}
                    except KeyError:
                        nxt_rids = set()
                if current_rids and nxt_rids and current_rids == nxt_rids:
                    return (current_reservation_id, current_resource_id)
        except Exception:
            pass
        # For QUOTA_EXHAUSTED we must preserve exhaustion (reconcile, not release)
        kind = str((classification or {}).get("kind") or "").upper()
        if kind == "QUOTA_EXHAUSTED":
            try:
                await self._reconcile_reservation_usage(current_reservation_id, current_resource_id, total_tokens=1)
            except Exception:
                try:
                    await self.quota_reservations.release(current_reservation_id)
                except Exception:
                    pass
        else:
            try:
                await self.quota_reservations.release(current_reservation_id)
            except Exception:
                pass
        # Try to reserve next candidate; if quota exhausted skip to subsequent ones
        for offset in range(1, len(candidates) - current_index):
            nxt_idx = current_index + offset
            nxt = candidates[nxt_idx]
            new_rid = f"{request_id}:{nxt_idx}" if request_id else __import__("uuid").uuid4().hex
            # _reserve_for_candidate handles fallback model: and empty rids
            try:
                rid, rsc, accepted = await self._reserve_for_candidate(nxt, route_name, body, new_rid)
            except Exception:
                continue
            if accepted:
                # rid is new_rid when accepted and has quota, or None when no quota needed
                # _reserve_for_candidate returns (reservation_id, resource_id, accepted)
                # For no-quota candidates it returns (None, None, True) — keep None
                return (rid, rsc)
            # not accepted -> quota exhausted, try next candidate (already released previous)
            # Need to clean up the rejected reservation entry? reserve_many stores rejected result under new_rid
            # No release needed for rejected (was not counted)
            continue
        # No remaining candidate could be reserved — return None to indicate no reservation
        return (None, None)

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
            # resource_key carries the canonical three-part schedulable identity
            # (connection:credential:model).  Runtime state must be keyed by it so
            # sibling credentials of the same connection+model never share state.
            # Kept as a separate field so the frozen snapshot metadata is untouched
            # and legacy equality on Candidate(upstream, model) still holds.
            candidates.append(Candidate(
                upstream=rc.resource_ref.provider_connection_id,
                model=rc.resource_ref.model_id,
                weight=rc.weight,
                metadata=rc.metadata,
                resource_key=rc.resource_ref.key,
            ))
        return candidates

    def _is_available(self, candidate: Candidate) -> bool:
        state = self.circuits.get(candidate.key)
        if state is not None and state.cooldown_until > time.monotonic():
            return False
        # If engine is authoritative, also check engine's circuit repo
        if self.router_engine is not None:
            from apps.gateway.routing.models import ResourceRef
            # Rebuild the canonical ref from candidate.key so the credential
            # dimension survives into the engine circuit lookup (plan §3.3).
            parts = candidate.key.split(":")
            ref = ResourceRef(
                provider_connection_id=parts[0],
                credential_scope=parts[1] if len(parts) > 2 else parts[0],
                model_id=parts[-1],
            )
            try:
                return self.router_engine.circuit_repository.is_available(ref)
            except Exception:
                pass
        return True

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
        # RouterEngine owns production scoring. Share this stateful store so
        # affinity recorded by request handling is visible during selection.
        engine_calculator = getattr(self.router_engine, "_score_calculator", None)
        if engine_calculator is not None:
            engine_calculator._session_store = self._session_store

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
            # Canonical key is connection:credential:model — the model is the
            # last segment (legacy 2-part keys keep working via rsplit).
            model_id = key.rsplit(":", 1)[-1] if ":" in key else ""
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
            model_id = key.rsplit(":", 1)[-1] if ":" in key else ""
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
            model_id = key.rsplit(":", 1)[-1] if ":" in key else ""
            price_info = prices.get(model_id, {})
            fail_rate, total_attempts, total_successes = self._failure_tracker.failure_rate(key)
            p50, p99, mean = self._latency_tracker.percentiles(key)
            latency_count = self._latency_tracker.count(key)
            effective_remaining = 0
            limit = 0
            safety_buffer = 0
            burn_urgency = 0.0
            normalized_pressure = 0.0
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
                    if lim > 0:
                        normalized_pressure = max(normalized_pressure, 1.0 - (eff / lim))
            if limit > 0 and effective_remaining >= 0:
                burn_urgency = normalized_pressure
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
                normalized_pressure=normalized_pressure,
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
        request_id = None
        if incoming_headers is not None:
            try:
                request_id = incoming_headers.get("x-request-id") or incoming_headers.get("X-Request-ID")
            except AttributeError:
                request_id = None
        request_id = str(request_id or uuid.uuid4().hex)
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

        # Session hint: chỉ lấy ID để scoring affinity — không đụng payload/secret
        conversation_thread = self._conversation_thread_hint(body, incoming_headers)
        required_capabilities = self._required_capabilities_from_body(body)

        candidates = await self._candidate_order(
            route_name,
            conversation_thread=conversation_thread,
            required_capabilities=required_capabilities,
        )
        reservation_id = None
        resource_id = None
        # Reservation theo candidate vật lý đã chọn (README §15.8) — thử reserve theo thứ tự candidate đã xếp hạng.
        # Giữ fallback route-level (model:{route}) để tương thích test cũ khi candidate chưa khai resource riêng.
        if not candidates:
            if self.quota_reservations is not None:
                # Kiểm tra xem empty có phải do quota exhausted không để trả đúng error type
                raw_route = self.routes.get(route_name, {})
                raw_cands = list(raw_route.get("candidates", [])) + list(raw_route.get("fallback", []))
                quota_blocked = False
                explicit_quota_resource = False
                for rc in raw_cands:
                    explicit_quota_resource = explicit_quota_resource or bool(
                        rc.metadata.get("quota_resource_id") or rc.metadata.get("quota_resource_ids")
                    )
                    rids = await self._known_candidate_quota_resource_ids(rc)
                    if not rids:
                        # fallback route-level resource nếu candidate chưa khai
                        try:
                            await self.quota_reservations.snapshot(f"model:{route_name}")
                            rids = [f"model:{route_name}"]
                        except KeyError:
                            rids = []
                    if not rids:
                        continue
                    from apps.gateway.quota.reservations import QuotaReservationRequest
                    try:
                        _check_reqs = []
                        for _rid in rids:
                            _risk = 0
                            try:
                                _r = await self.quota_reservations.snapshot(_rid)
                                _amt = self._quota_request_amount(
                                    _r,
                                    self._estimate_input_tokens(body),
                                    int(body.get("max_tokens", 0) or 0),
                                )
                                _risk = self._quota_risk_buffer(_r, _amt, route_name)
                            except Exception:
                                _amt = 1
                                _risk = 0
                            _check_reqs.append(QuotaReservationRequest(_rid, amount=_amt, risk_buffer=_risk))
                        adm = await self.quota_reservations.check_many(_check_reqs)
                    except KeyError:
                        continue
                    if not adm.accepted:
                        quota_blocked = True
                        break
                if quota_blocked and explicit_quota_resource:
                    await self._record_usage_request(
                        request_id=request_id,
                        route_name=route_name,
                        metadata={"status": "rejected", "reason": "quota_exhausted", "path": path},
                    )
                    return JSONResponse(
                        status_code=503,
                        content={"error": {"type": "quota_exhausted", "message": "quota exhausted"}},
                    )
            await self._record_usage_request(
                request_id=request_id,
                route_name=route_name,
                metadata={"status": "rejected", "reason": "overloaded", "path": path},
            )
            return JSONResponse(
                status_code=503,
                content={"error": {"type": "overloaded", "message": "all candidates are cooling down or unavailable"}},
            )
        # Estimated tokens for TPM/token quota (README §15.8) — compute before reservation
        _estimated_for_quota = self._estimate_input_tokens(body)
        try:
            _estimated_output_for_quota = max(0, int(body.get("max_tokens", 0) or 0))
        except (TypeError, ValueError):
            _estimated_output_for_quota = 0
        if self.quota_reservations is not None:
            from apps.gateway.quota.reservations import QuotaReservationRequest
            reserved = False
            for idx, cand in enumerate(list(candidates)):
                rids = await self._known_candidate_quota_resource_ids(cand)
                if not rids:
                    try:
                        await self.quota_reservations.snapshot(f"model:{route_name}")
                        rids = [f"model:{route_name}"]
                    except KeyError:
                        rids = []
                if not rids:
                    reservation_id = None
                    resource_id = None
                    reserved = True
                    if idx != 0:
                        candidates = [cand] + candidates[:idx] + candidates[idx+1:]
                    break
                tmp_id = request_id if idx == 0 else f"{request_id}:{idx}"
                # Build amount per metric: requests=1, tokens/tpm=estimated_input_tokens
                reqs: list[QuotaReservationRequest] = []
                for rid in rids:
                    risk = 0
                    try:
                        _res = await self.quota_reservations.snapshot(rid)
                        amt = self._quota_request_amount(
                            _res,
                            _estimated_for_quota,
                            _estimated_output_for_quota,
                        )
                        risk = self._quota_risk_buffer(_res, amt, route_name)
                    except Exception:
                        amt = 1
                        risk = 0
                    reqs.append(QuotaReservationRequest(rid, amount=amt, risk_buffer=risk))
                try:
                    res = await self.quota_reservations.reserve_many(reservation_id=tmp_id, requests=reqs)
                except (KeyError, ValueError):
                    reservation_id = None
                    resource_id = None
                    reserved = True
                    if idx != 0:
                        candidates = [cand] + candidates[:idx] + candidates[idx+1:]
                    break
                if res.accepted:
                    reservation_id = tmp_id
                    resource_id = ",".join(rids)
                    reserved = True
                    if idx != 0:
                        candidates = [cand] + candidates[:idx] + candidates[idx+1:]
                    break
                # rejected -> thử candidate tiếp theo (không giữ reservation rejected)
                continue
            if not reserved:
                await self._record_usage_request(
                    request_id=request_id,
                    route_name=route_name,
                    metadata={"status": "rejected", "reason": "quota_exhausted", "path": path},
                )
                return JSONResponse(
                    status_code=503,
                    content={"error": {"type": "quota_exhausted", "message": "quota exhausted"}},
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
                        reservation_id, resource_id = await self._maybe_transfer_reservation(
                            reservation_id, resource_id, candidates, index, route_name, body, request_id, classification,
                        )
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
                        reservation_id, resource_id = await self._maybe_transfer_reservation(
                            reservation_id, resource_id, candidates, index, route_name, body, request_id, classification,
                        )
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
                        await self._reconcile_reservation_usage(
                            reservation_id, resource_id, total_tokens=total_tokens,
                        )
                    else:
                        await self._reconcile_reservation_usage(
                            reservation_id, resource_id, total_tokens=1,
                        )
                elapsed_ms = (time.monotonic() - _start) * 1000
                if self._score_calculator:
                    self._latency_tracker.record(candidate.key, elapsed_ms)
                self._remember_session_affinity(conversation_thread, candidate)
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
                    reservation_id, resource_id = await self._maybe_transfer_reservation(
                        reservation_id, resource_id, candidates, index, route_name, body, request_id,
                    )
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
                    reservation_id, resource_id = await self._maybe_transfer_reservation(
                        reservation_id, resource_id, candidates, index, route_name, body, request_id,
                    )
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
                    await self._reconcile_reservation_usage(
                            reservation_id, resource_id, total_tokens=total_tokens,
                        )
                else:
                    await self._reconcile_reservation_usage(
                            reservation_id, resource_id, total_tokens=1,
                        )
            elapsed_ms = (time.monotonic() - _start) * 1000
            if self._score_calculator:
                self._latency_tracker.record(candidate.key, elapsed_ms)
            self._remember_session_affinity(conversation_thread, candidate)
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
                        reservation_id, resource_id = await self._maybe_transfer_reservation(
                            reservation_id, resource_id, candidates, index, route_name, body, request_id, classification,
                        )
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
                    self._remember_session_affinity(conversation_thread, _candidate)
                    if reservation_id is not None and resource_id is not None:
                        tokens = self._get_latest_usage_tokens(attempt_id=attempt_id)
                        total_tokens = tokens["total_tokens"]
                        if total_tokens > 0:
                            await self._reconcile_reservation_usage(
                            reservation_id, resource_id, total_tokens=total_tokens,
                        )
                        else:
                            # No usage parsed — fall back to 1 request as before
                            await self._reconcile_reservation_usage(
                            reservation_id, resource_id, total_tokens=1,
                        )
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
        if cooldown_seconds is None or cooldown_seconds <= 0:
            return
        # Update legacy SmartRouter circuit state
        if isinstance(cooldown_seconds, float):
            # Canonical identity includes connection+credential+model.
            # State is keyed by ref.key to isolate sibling credentials.
            state = self.circuits.setdefault(candidate.key, CircuitState())
            state.cooldown_until = time.monotonic() + cooldown_seconds
        # Also trip the canonical engine circuit repository
        if self.router_engine is not None:
            try:
                from apps.gateway.routing.models import ResourceRef
                # Construct ref from the candidate's canonical resource_key or legacy parts
                parts = candidate.key.split(":")
                ref = ResourceRef(
                    provider_connection_id=parts[0],
                    credential_scope=parts[1] if len(parts) > 2 else parts[0],
                    model_id=parts[-1]
                )
                self.router_engine.circuit_repository.trip(ref, cooldown_seconds)
            except Exception:
                pass

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
                await self._reconcile_reservation_usage(
                            reservation_id, resource_id, total_tokens=1,
                        )
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
        """Delegate catalog sync to the provider-agnostic worker."""
        result = await self.catalog_worker.sync_catalog(initial=initial)
        self.catalog = self.catalog_worker.catalog
        selected = self.catalog.get("selected_routes", {}) if isinstance(self.catalog, dict) else {}
        if isinstance(selected, dict):
            self._apply_generated_routes(selected)
        return result

    def _apply_generated_routes(self, selected: dict[str, list[str]]) -> None:
        """Apply worker-generated catalog routes without provider branching."""
        self.catalog_worker.apply_generated_routes(self.routes, selected)

    def _state_path(self, setting: str) -> Path:
        return self.catalog_worker._state_path(setting)

    def _load_catalog(self) -> dict[str, Any]:
        catalog = self.catalog_worker.catalog
        selected = catalog.get("selected_routes", {}) if isinstance(catalog, dict) else {}
        if isinstance(selected, dict) and selected:
            self._apply_generated_routes(selected)
        return catalog

    def _persist_catalog(self, state: dict[str, Any], selected: dict[str, list[str]]) -> None:
        self.catalog_worker._persist_catalog(state, selected)


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
    await dispose_engine()  # close pools; no-op if nothing was created


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
    return {
        "status": "ok",
        "service": "smart-router",
        "version": app.version,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT_MONO),
    }


def _dependency_health_payload(
    *,
    database_url: str | None = None,
    redis_url: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return dependency readiness metadata without exposing connection URLs.

    The endpoint must stay non-blocking: it reports configuration presence,
    while connection failures remain visible through startup logs and backend
    specific metrics. Do not include URLs, usernames, or passwords here.
    """
    database_configured = bool(database_url if database_url is not None else os.getenv("DATABASE_URL"))
    redis_configured = bool(redis_url if redis_url is not None else os.getenv("REDIS_URL"))
    return {
        "database": {"status": "unknown", "configured": database_configured},
        "redis": {"status": "unknown", "configured": redis_configured},
    }


def _quota_backend_kind(service: Any | None) -> str:
    if service is not None:
        quota = getattr(service, "quota_reservations", None)
        if quota is not None:
            name = quota.__class__.__name__
            if "Redis" in name:
                return "redis"
            # AsyncQuotaFacade wrapping InMemory -> treat as memory
            backend = getattr(quota, "_backend", None)
            if backend is not None and "Redis" in backend.__class__.__name__:
                return "redis"
    return "memory"


@app.get("/health/ready")
async def health_ready(request: Request) -> dict[str, Any]:
    service = getattr(request.app.state, "router", None)
    upstream_count = len(service.clients) if service is not None else 0
    qkind = _quota_backend_kind(service)
    return {
        "status": "ok",
        "service": "smart-router",
        "version": app.version,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT_MONO),
        "upstream_count": upstream_count,
        "checks": {
            "upstreams": {"status": "ok", "count": upstream_count},
            "quota": {"status": "ok", "backend": qkind},
            **_dependency_health_payload(),
        },
    }


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    service = getattr(request.app.state, "router", None)
    upstream_count = len(service.clients) if service is not None and hasattr(service, "clients") else 0
    return {
        "status": "ok",
        "service": "smart-router",
        "version": app.version,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT_MONO),
        "upstream_count": upstream_count,
    }


@app.get("/version")
async def version() -> dict[str, Any]:
    return {"service": "smart-router", "version": app.version}


@app.get("/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    service = getattr(request.app.state, "router", None)
    upstream_count = len(service.clients) if service is not None and hasattr(service, "clients") else 0
    return {
        "service": "smart-router",
        "version": app.version,
        "uptime_seconds": int(time.monotonic() - _STARTED_AT_MONO),
        "upstream_count": upstream_count,
        "upstreams": upstream_count,
        "quota_backend": _quota_backend_kind(service),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/models")
async def models(service: SmartRouter = Depends(get_authorized_service)) -> dict[str, Any]:
    data = [
        {"id": name, "object": "model", "owned_by": "smart-router", "display_name": name}
        for name in service.routes
    ]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    service: SmartRouter = Depends(get_authorized_service),
    usage_ledger: Any | None = Depends(get_optional_usage_ledger_repo),
) -> Response:
    """OpenAI Chat Completions compatibility endpoint.

    The router core remains Messages-native. This boundary translates only the
    public request/response envelope, keeping candidate selection, retries,
    quota accounting, and provider drivers on the same execution path.
    """
    try:
        openai_body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    if not isinstance(openai_body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    router_body = openai_request_to_router(openai_body)
    ledger_token = _REQUEST_USAGE_LEDGER.set(usage_ledger)
    events_token = _REQUEST_USAGE_EVENTS.set([])
    try:
        response = await service.handle_messages(router_body, request.headers, "/v1/messages")
        if response.status_code >= 400:
            return response
        requested_model = str(openai_body.get("model") or "")
        if bool(openai_body.get("stream", False)):
            async def openai_stream() -> AsyncIterator[str]:
                from apps.gateway.openai_compat import OpenAIStreamEncoder as _Enc
                encoder = _Enc(requested_model)
                buffer = ""
                async for raw_chunk in response.body_iterator:
                    buffer += raw_chunk.decode("utf-8", "replace") if isinstance(raw_chunk, bytes) else str(raw_chunk)
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        data_lines = [line[5:].strip() for line in frame.splitlines() if line.startswith("data:")]
                        if not data_lines:
                            continue
                        data = "\n".join(data_lines)
                        if data == "[DONE]":
                            continue
                        try:
                            event = json.loads(data)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        for converted in encoder.feed(event):
                            if converted != "data: [DONE]\n\n":
                                yield converted
                # drain any remaining partial buffer into encoder
                if buffer.strip():
                    data_lines = [line[5:].strip() for line in buffer.splitlines() if line.startswith("data:")]
                    try:
                        remaining_data = "\n".join(data_lines)
                        remaining_event = json.loads(remaining_data)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        remaining_event = None
                    if remaining_event:
                        for converted in encoder.feed(remaining_event):
                            if converted != "data: [DONE]\n\n":
                                yield converted
                for _chunk in encoder.finish():
                    yield _chunk

            return StreamingResponse(
                openai_stream(),
                status_code=response.status_code,
                media_type="text/event-stream",
                headers=_response_headers(response.headers),
            )
        try:
            payload = json.loads(response.body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return JSONResponse(
                status_code=502,
                content={"error": {"message": "upstream returned invalid JSON", "type": "server_error"}},
            )
        return JSONResponse(
            status_code=response.status_code,
            content=router_response_to_openai(payload, requested_model),
            headers=_response_headers(response.headers),
        )
    finally:
        _REQUEST_USAGE_EVENTS.reset(events_token)
        _REQUEST_USAGE_LEDGER.reset(ledger_token)


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


@app.post("/router/sync")
async def force_catalog_sync(service: SmartRouter = Depends(get_authorized_service)) -> Response:
    result = await service.sync_catalog()
    return JSONResponse(status_code=200 if result.get("ok") else 502, content=result)


@app.get("/router/catalog")
async def catalog_sync_state(service: SmartRouter = Depends(get_authorized_service)) -> dict[str, Any]:
    return {
        "catalog": service.catalog,
        "routes": {
            name: [candidate.model for candidate in route["candidates"]]
            for name, route in service.routes.items()
            if route.get("generated")
        },
    }


# --- OpenAI /v1/responses compatibility ---


@app.post("/v1/responses")
async def responses(
    request: Request,
    service: SmartRouter = Depends(get_authorized_service),
    usage_ledger: Any | None = Depends(get_optional_usage_ledger_repo),
) -> Response:
    """OpenAI Responses API compatibility endpoint."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    model = str(body.get("model", "default-route"))
    input_data = body.get("input", [])
    messages = []
    for item in input_data if isinstance(input_data, list) else [input_data]:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
        elif isinstance(item, dict):
            role = item.get("role", "user")
            content = item.get("content", "")
            messages.append({"role": role, "content": content})
    router_body = {
        "model": model,
        "messages": messages,
        **{k: v for k, v in body.items() if k not in ("model", "input")},
    }
    ledger_token = _REQUEST_USAGE_LEDGER.set(usage_ledger)
    events_token = _REQUEST_USAGE_EVENTS.set([])
    try:
        response = await service.handle_messages(router_body, request.headers, "/v1/responses")
        if response.status_code >= 400:
            return response
        payload = json.loads(response.body) if hasattr(response, "body") and response.body else {}
        # Translate native Messages envelope → OpenAI Responses envelope
        output_items = payload.get("content", [{"type": "text", "text": ""}])
        output_list = []
        for oi in (output_items if isinstance(output_items, list) else [output_items]):
            if isinstance(oi, dict):
                output_list.append({"object": "message", "role": "assistant", "content": [oi]})
            else:
                output_list.append({"object": "message", "role": "assistant", "content": [{"type": "text", "text": str(oi)}]})
        usage = payload.get("usage", {})
        total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        openai_resp = {
            "id": payload.get("id", "res-smart-router"),
            "object": "response",
            "model": model,
            "created_at": int(time.time()) if "time" in dir() else 0,
            "output": output_list,
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0), "total_tokens": total},
        }
        openai_resp["created_at"] = int(time.time())
        return JSONResponse(
            status_code=response.status_code,
            content=openai_resp,
            headers=_response_headers(response.headers) if hasattr(response, "headers") else None,
        )
    finally:
        _REQUEST_USAGE_EVENTS.reset(events_token)
        _REQUEST_USAGE_LEDGER.reset(ledger_token)


@app.get("/router/status")
async def router_status(service: SmartRouter = Depends(get_authorized_service)) -> dict[str, Any]:
    return service.status_payload()
