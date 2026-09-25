"""Central DriverContext factory: strict selection of canonical credentials.

Proves test/discover/execute paths resolve the SAME credential and header
source for the same (connection, credential_id) input. Ciphertexts are
produced with the repo's own Fernet crypto against test-only plaintext, so
committed fixtures contain no secrets.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.gateway.providers.driver_context import (
    DriverContext,
    build_driver_context,
)
from apps.gateway.security.crypto import encrypt_secret


class FakeRepo:
    """Duck-typed ProviderRegistryRepository: same find/list/resolve surface."""

    def __init__(self, credentials):
        self.credentials = credentials

    async def list_credentials(self, connection_id):
        return [c for c in self.credentials if c.connection_id == connection_id]

    async def find_credential(self, connection_id, credential_id):
        return next(
            (
                c
                for c in self.credentials
                if c.connection_id == connection_id and c.id == credential_id
            ),
            None,
        )

    async def resolve_credential(self, connection):
        for credential in await self.list_credentials(connection.id):
            if credential.enabled:
                return credential.credential_encrypted
        return connection.credential_encrypted


def _connection(active=True):
    return SimpleNamespace(
        id="conn-1",
        base_url="https://provider.test/v1",
        driver="generic-openai",
        template_id="openai",
        is_active=active,
        credential_encrypted=None,
    )


def _credential(credential_id, plaintext, *, enabled=True, priority=0):
    return SimpleNamespace(
        id=credential_id,
        connection_id="conn-1",
        credential_encrypted=encrypt_secret(plaintext),
        enabled=enabled,
        priority=priority,
    )


@pytest.mark.asyncio
async def test_factory_resolves_requested_enabled_credential_not_default():
    first = _credential("cred-first", "test-key-first", priority=10)
    second = _credential("cred-second", "test-key-second", priority=0)
    repo = FakeRepo([first, second])

    context = await build_driver_context(repo, _connection(), credential_id=second.id)

    assert context.credential_id == second.id
    assert context.credential == {"api_key": "test-key-second"}
    # dict compatibility for legacy drivers
    assert context.get("credential_id") == second.id
    assert context.get("credential") == {"api_key": "test-key-second"}
    assert context["base_url"] == "https://provider.test/v1"


@pytest.mark.asyncio
async def test_factory_rejects_requested_credential_from_other_connection_or_disabled():
    disabled = _credential("cred-disabled", "test-key-disabled", enabled=False)
    other = SimpleNamespace(
        id="cred-other",
        connection_id="conn-other",
        credential_encrypted=encrypt_secret("test-key-other"),
        enabled=True,
        priority=0,
    )
    default = _credential("cred-default", "test-key-default")
    repo = FakeRepo([disabled, other, default])

    with pytest.raises(ValueError, match="credential not found or disabled"):
        await build_driver_context(repo, _connection(), credential_id=disabled.id)
    with pytest.raises(ValueError, match="credential not found or disabled"):
        await build_driver_context(repo, _connection(), credential_id=other.id)
    with pytest.raises(ValueError, match="credential not found or disabled"):
        await build_driver_context(repo, _connection(), credential_id="cred-missing")


@pytest.mark.asyncio
async def test_factory_rejects_disabled_connection():
    repo = FakeRepo([_credential("cred-first", "test-key-first")])

    with pytest.raises(ValueError, match="provider connection is disabled"):
        await build_driver_context(repo, _connection(active=False))


@pytest.mark.asyncio
async def test_no_explicit_id_selects_highest_priority_enabled_default():
    low = _credential("cred-low", "test-key-low", priority=0)
    high = _credential("cred-high", "test-key-high", priority=10)
    repo = FakeRepo([low, high])

    context = await build_driver_context(repo, _connection())

    assert context.credential_id == high.id
    assert context.credential == {"api_key": "test-key-high"}


@pytest.mark.asyncio
async def test_test_discover_and_execute_contexts_share_credential_and_headers():
    """Admin test/discover (no explicit id) and execute (explicit id) built from
    the same credential must yield the same credential + header source."""
    default = _credential("cred-a", "test-key-a", priority=5)
    sibling = _credential("cred-b", "test-key-b", priority=1)
    repo = FakeRepo([default, sibling])
    headers = {"x-tenant": "acme"}

    admin_ctx = await build_driver_context(repo, _connection(), headers=headers)
    execute_ctx = await build_driver_context(
        repo, _connection(), credential_id=default.id, headers=headers
    )

    assert execute_ctx.credential == admin_ctx.credential
    assert execute_ctx.get("credential") == admin_ctx.get("credential")
    assert execute_ctx.connection == admin_ctx.connection == {"headers": {"x-tenant": "acme"}}

    other_execute_ctx = await build_driver_context(
        repo, _connection(), credential_id=sibling.id, headers=headers
    )
    assert other_execute_ctx.credential == {"api_key": "test-key-b"}
    assert other_execute_ctx.credential_id == sibling.id

    # drivers reading auth through the http_base helper see the same source
    from apps.gateway.providers.http_base import credential_value

    assert credential_value(admin_ctx, "api_key") == credential_value(execute_ctx, "api_key")
    assert credential_value(other_execute_ctx, "api_key") == "test-key-b"


@pytest.mark.asyncio
async def test_factory_falls_back_to_legacy_connection_credential_column():
    legacy_conn = SimpleNamespace(
        id="conn-legacy",
        base_url="https://legacy.test/v1",
        driver="generic-openai",
        template_id="openai",
        is_active=True,
        credential_encrypted=encrypt_secret("test-key-legacy"),
    )
    repo = FakeRepo([])

    context = await build_driver_context(repo, legacy_conn)

    assert context.credential == {"api_key": "test-key-legacy"}
    assert context.credential_id == ""


@pytest.mark.asyncio
async def test_factory_does_not_log_secret_material(caplog):
    cred = _credential("cred-x", "super-secret-value")
    repo = FakeRepo([cred])

    with caplog.at_level("DEBUG"):
        await build_driver_context(repo, _connection(), credential_id=cred.id)

    assert "super-secret-value" not in caplog.text
    assert cred.credential_encrypted not in caplog.text


def test_driver_context_keeps_plain_dict_compatibility():
    legacy = {"connection_id": "c", "base_url": "b", "credential": {"api_key": "k"}}
    context = DriverContext.from_dict(legacy)

    assert context.connection_id == "c"
    assert context.credential_id == ""
    assert dict(context)["connection_id"] == "c"
    assert context.get("missing", "fallback") == "fallback"
    assert DriverContext.from_dict(context) is context
