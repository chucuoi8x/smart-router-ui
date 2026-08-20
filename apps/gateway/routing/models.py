from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class ResourceRef:
    provider_connection_id: str
    credential_scope: str
    model_id: str

@dataclass(frozen=True)
class ResourceCandidate:
    resource_ref: ResourceRef
    driver_id: str
    weight: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Capability:
    value: Any
    source: str
    confidence: str
