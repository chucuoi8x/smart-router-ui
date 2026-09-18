from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from apps.gateway.routing.models import ResourceCandidate


class FrozenList(tuple):
    """Immutable sequence that also compares equal to equivalent lists/tuples.

    Subclasses tuple so existing type checks (isinstance(x, (list, tuple)))
    in the routing engine keep working while snapshot contents stay immutable.
    """

    __slots__ = ()

    def __new__(cls, values=()):
        return super().__new__(cls, values)

    def __eq__(self, other):
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return super().__hash__()

    def __repr__(self):
        return repr(list(self))


def _deep_freeze(value: Any) -> Any:
    """Recursively convert dicts -> MappingProxyType, lists -> FrozenList."""
    if isinstance(value, MappingABC):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return FrozenList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _freeze_candidate(candidate: ResourceCandidate) -> ResourceCandidate:
    """Return a copy of the candidate whose metadata is deep-frozen."""
    return ResourceCandidate(
        resource_ref=candidate.resource_ref,
        driver_id=candidate.driver_id,
        weight=candidate.weight,
        metadata=_deep_freeze(candidate.metadata),
    )


@dataclass(frozen=True)
class ConnectionConfig:
    connection_id: str
    base_url: str
    auth_mode: str
    token_env: str


@dataclass(frozen=True)
class RouteConfig:
    route_name: str
    strategy: str
    candidates: tuple = ()
    fallback: tuple = ()
    generated: bool = False

    def __post_init__(self):
        object.__setattr__(
            self, "candidates", tuple(_freeze_candidate(c) for c in self.candidates)
        )
        object.__setattr__(
            self, "fallback", tuple(_freeze_candidate(c) for c in self.fallback)
        )


@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    connections: Mapping[str, ConnectionConfig] = field(default_factory=dict)
    routes: Mapping[str, RouteConfig] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.connections, MappingProxyType):
            object.__setattr__(self, "connections", MappingProxyType(dict(self.connections)))
        if not isinstance(self.routes, MappingProxyType):
            object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))
