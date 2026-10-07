"""FIX-01 guard: RuntimeConfigManager is the only activation authority.

Behavior tests in :mod:`test_p0_20_runtime_authority` prove the Admin endpoints
route through ``manager.activate``.  This test locks the invariant in place so a
future Admin endpoint cannot reintroduce a direct ``RevisionRepository.activate``
call and leave the DB active pointer disconnected from the live runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path

ADMIN_SOURCE = Path(__file__).resolve().parents[2] / "apps" / "gateway" / "api" / "admin.py"

# ``_ensure_active_revision`` is the documented bootstrap/migration utility: it
# seeds an empty active revision when the DB has none, before any manager exists.
_ALLOWED_REPOSITORY_ACTIVATION_OWNERS = {"_ensure_active_revision"}


def _repository_activation_owners() -> list[str]:
    """Return admin endpoint helpers that call ``RevisionRepository.activate`` directly."""
    tree = ast.parse(ADMIN_SOURCE.read_text(encoding="utf-8"), filename=str(ADMIN_SOURCE))
    owners: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr != "activate" or call.args:
                continue
            base = call.func.value
            if isinstance(base, ast.Name) and base.id in {"rev_repo", "repo"}:
                owners.append(node.name)
                break
    return owners


def test_admin_module_has_no_direct_revision_repository_activation():
    offenders = sorted(set(_repository_activation_owners()) - _ALLOWED_REPOSITORY_ACTIVATION_OWNERS)
    assert offenders == [], (
        f"Admin endpoints must activate through RuntimeConfigManager.activate, got: {offenders}"
    )
