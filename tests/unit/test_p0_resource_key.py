"""P0-06 RED tests: canonical resource identity includes credential scope."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.routing.engine import InMemoryCircuitRepository
from apps.gateway.routing.models import ResourceRef


def test_resource_ref_canonical_key_includes_connection_credential_model():
    ref = ResourceRef("conn-a", "cred-a", "model-x")
    assert ref.key == "conn-a:cred-a:model-x"


def test_circuit_state_isolated_between_credentials():
    repo = InMemoryCircuitRepository()
    cred_a = ResourceRef("conn-a", "cred-a", "model-x")
    cred_b = ResourceRef("conn-a", "cred-b", "model-x")

    repo.trip(cred_a, cooldown_seconds=60)

    assert repo.is_available(cred_a) is False
    assert repo.is_available(cred_b) is True
