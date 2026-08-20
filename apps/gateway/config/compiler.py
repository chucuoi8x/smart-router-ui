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

        # Parse connections (upstreams)
        connections: Dict[str, ConnectionConfig] = {}
        for conn_name, conn_data in config.get('upstreams', {}).items():
            auth = conn_data.get('auth', {})
            connections[conn_name] = ConnectionConfig(
                connection_id=conn_name,
                base_url=conn_data.get('base_url', ''),
                auth_mode=auth.get('mode', 'bearer'),
                token_env=auth.get('token_env', '')
            )

        # Parse routes
        routes: Dict[str, RouteConfig] = {}
        for route_name, route_data in config.get('routes', {}).items():
            candidates = []
            for item in route_data.get('candidates', []):
                upstream = item.get('upstream')
                model = item.get('model')
                weight = item.get('weight', 1)
                ref = ResourceRef(
                    provider_connection_id=upstream,
                    credential_scope=upstream,
                    model_id=model
                )
                candidates.append(ResourceCandidate(ref, driver_id='anthropic-compatible', weight=weight))

            fallback = []
            for item in route_data.get('fallback', []):
                upstream = item.get('upstream')
                model = item.get('model')
                ref = ResourceRef(
                    provider_connection_id=upstream,
                    credential_scope=upstream,
                    model_id=model
                )
                fallback.append(ResourceCandidate(ref, driver_id='anthropic-compatible', weight=1))

            routes[route_name] = RouteConfig(
                route_name=route_name,
                strategy=route_data.get('strategy', 'priority'),
                candidates=candidates,
                fallback=fallback,
                generated=route_data.get('generated', False)
            )

        return RuntimeConfigSnapshot(connections=connections, routes=routes)
