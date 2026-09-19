"""RED tests Step 115 — mã hóa credential at-rest AC-14."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_credential_crypto_encrypt_decrypt_roundtrip():
    from apps.gateway.security.crypto import decrypt_secret, encrypt_secret

    plaintext = "sk-test-115-secret-value"
    encrypted = encrypt_secret(plaintext)
    assert encrypted != plaintext
    assert encrypted != ""
    assert len(encrypted) > len(plaintext)
    assert decrypt_secret(encrypted) == plaintext


def test_credential_crypto_does_not_store_plaintext():
    from apps.gateway.security.crypto import encrypt_secret

    enc = encrypt_secret("sk-another-secret")
    assert "sk-another-secret" not in enc


@pytest.mark.asyncio
async def test_create_provider_encrypted_storage_redacted():
    from router import app
    from apps.gateway.security.crypto import decrypt_secret

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = {"Authorization": "Bearer test-admin-key"}
        plaintext = "sk-115-provider-secret-xyz"
        created = await client.post(
            "/api/admin/v1/providers",
            json={"template_id": "openai", "name": "prov-crypto-test", "base_url": "https://api.openai.com/v1", "api_key": plaintext},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        cid = body["connection_id"]
        # response không lộ plaintext
        assert plaintext not in created.text
        assert "api_key" not in created.text.lower()
        # PR-05: credential persists in the database, never in process globals.
        # Storage must hold ciphertext that decrypts back to the plaintext.
        from apps.gateway.db.session import get_async_session_factory
        from apps.gateway.db.models import ProviderConnection as _PC
        from sqlalchemy import select
        # Use current event loop via get_running_loop/await directly
        factory = get_async_session_factory()
        async with factory() as session:
            row = (await session.execute(select(_PC).where(_PC.id == cid))).scalar_one()
            enc = row.credential_encrypted or ""
        assert enc, "credential_encrypted phải được lưu"
        assert plaintext not in enc
        assert decrypt_secret(enc) == plaintext

        # GET detail cũng không lộ
        detail = await client.get(f"/api/admin/v1/providers/{cid}", headers=headers)
        assert detail.status_code == 200
        assert plaintext not in detail.text
        assert "api_key" not in detail.text.lower()

        # audit cũng không lộ
        audit = await client.get("/api/admin/v1/audit", headers=headers)
        assert plaintext not in audit.text
