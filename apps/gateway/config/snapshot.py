from dataclasses import dataclass, field
from typing import Any, Dict
from apps.gateway.routing.models import ResourceCandidate

@dataclass
class ConnectionConfig:
    connection_id: str
    base_url: str
    auth_mode: str
    token_env: str

@dataclass
class RouteConfig:
    route_name: str
    strategy: str
    candidates: list[ResourceCandidate] = field(default_factory=list)
    fallback: list[ResourceCandidate] = field(default_factory=list)
    generated: bool = False

@dataclass
class RuntimeConfigSnapshot:
    connections: Dict[str, ConnectionConfig] = field(default_factory=dict)
    routes: Dict[str, RouteConfig] = field(default_factory=dict)
