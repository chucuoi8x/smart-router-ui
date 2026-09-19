import yaml
from pathlib import Path
from typing import Dict

from apps.gateway.routing.models import ResourceRef, ResourceCandidate
from apps.gateway.config.snapshot import ConnectionConfig, RouteConfig, RuntimeConfigSnapshot

class LegacyConfigCompiler:
    def compile_file(self, file_path: str | Path) -> RuntimeConfigSnapshot:
        path = Path(file_path)
        with open(path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        return self.compile_dict(config)

    def compile_dict(self, config_dict: dict) -> RuntimeConfigSnapshot:
        config = config_dict or {}
        connections: Dict[str, ConnectionConfig] = {}
        for conn_name, conn_data in config.get('upstreams', {}).items():
            auth = conn_data.get('auth', {})
            connections[conn_name] = ConnectionConfig(
                connection_id=conn_name,
                base_url=conn_data.get('base_url', ''),
                auth_mode=auth.get('mode', 'bearer'),
                token_env=auth.get('token_env', '')
            )

        routes: Dict[str, RouteConfig] = {}
        for route_name, route_data in config.get('routes', {}).items():
            candidates = []
            for item in route_data.get('candidates', []):
                upstream = item.get('upstream')
                model = item.get('model')
                weight = item.get('weight', 1)
                # Canonical schedulable identity is connection+credential+model
                # (plan §3.3).  A candidate may pin its credential via
                # ``credential_id``; otherwise the connection's default
                # credential scope stands.
                credential = item.get('credential_id') or item.get('credential_scope') or upstream
                ref = ResourceRef(
                    provider_connection_id=upstream,
                    credential_scope=str(credential),
                    model_id=model
                )
                metadata = self._candidate_metadata(item)
                candidates.append(ResourceCandidate(ref, driver_id='anthropic-compatible', weight=weight, metadata=metadata))

            fallback = []
            for item in route_data.get('fallback', []):
                upstream = item.get('upstream')
                model = item.get('model')
                credential = item.get('credential_id') or item.get('credential_scope') or upstream
                ref = ResourceRef(
                    provider_connection_id=upstream,
                    credential_scope=str(credential),
                    model_id=model
                )
                metadata = self._candidate_metadata(item)
                fallback.append(ResourceCandidate(ref, driver_id='anthropic-compatible', weight=1, metadata=metadata))

            routes[route_name] = RouteConfig(
                route_name=route_name,
                strategy=route_data.get('strategy', 'priority'),
                candidates=candidates,
                fallback=fallback,
                generated=route_data.get('generated', False)
            )

        return RuntimeConfigSnapshot(connections=connections, routes=routes)

    def _candidate_metadata(self, item: dict) -> dict:
        metadata = {}
        for key in ("quota_resource_id", "quota_resource_ids"):
            if key in item:
                metadata[key] = item[key]
        # Policy constraint metadata: phải giữ để filter policy hoạt động đúng
        for key in ("quality_score", "is_paid", "expected_cost_per_request"):
            if key in item:
                metadata[key] = item[key]
        # Smart scoring hints
        for key in ("session_group", "driver_id"):
            val = item.get(key)
            if val is not None:
                metadata[key] = val
        # M5 feature metrics/capability: giữ để scoring và eligibility có dữ liệu, fail-open nếu thiếu
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
