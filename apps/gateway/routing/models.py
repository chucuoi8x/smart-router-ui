from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class ResourceRef:
    """Canonical schedulable-resource identity.

    Every runtime state key must include connection, credential and model so a
    credential-scoped failure cannot poison sibling credentials.
    """
    provider_connection_id: str
    credential_scope: str
    model_id: str

    @property
    def key(self) -> str:
        return f"{self.provider_connection_id}:{self.credential_scope}:{self.model_id}"

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
