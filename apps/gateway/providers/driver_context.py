"""Driver context and factory for provider drivers."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Callable

from apps.gateway.security.crypto import decrypt_secret


@dataclass
class DriverContext(Mapping[str, Any]):
    """Context passed to driver methods.

    Mapping behavior preserves compatibility with drivers that still use
    ``ctx.get(...)`` or subscript access.
    """

    connection_id: str = ""
    credential_id: str = ""
    base_url: str = ""
    driver: str = ""
    template_id: str = ""
    credential: dict[str, Any] = field(default_factory=dict)
    connection: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if key not in {item.name for item in fields(self)}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(item.name for item in fields(self))

    def __len__(self) -> int:
        return len(fields(self))

    @classmethod
    def from_dict(cls, ctx: dict[str, Any] | DriverContext) -> DriverContext:
        """Convert plain dict ctx to DriverContext, or return an existing one."""
        if isinstance(ctx, cls):
            return ctx
        if isinstance(ctx, dict):
            return cls(
                connection_id=ctx.get("connection_id", ""),
                credential_id=ctx.get("credential_id", ""),
                base_url=ctx.get("base_url", ""),
                driver=ctx.get("driver", ""),
                template_id=ctx.get("template_id", ""),
                credential=ctx.get("credential", {}),
                connection=ctx.get("connection", {}),
                runtime=ctx.get("runtime", {}),
            )
        raise TypeError(f"Expected dict or DriverContext, got {type(ctx)}")


def _resolve_order_key(credential: Any) -> tuple:
    """Sort key replicating the repository's default-credential ORDER BY."""
    try:
        created_ts = credential.created_at.timestamp()
    except AttributeError:
        created_ts = 0.0
    return (
        -int(credential.priority or 0),
        created_ts,
        str(credential.id),
    )


async def build_driver_context(
    repo: Any,
    connection: Any,
    *,
    credential_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    runtime: Mapping[str, Any] | None = None,
    decrypt: Callable[[str], str] = decrypt_secret,
) -> DriverContext:
    """Build canonical provider context for control-plane or data-plane use.

    Explicit credential IDs are strict: credential must belong to connection
    and be enabled. Missing or disabled explicit credentials never fall back to
    connection default.
    """
    if not bool(getattr(connection, "is_active", False)):
        raise ValueError("provider connection is disabled")

    selected = None
    if credential_id:
        selected = await repo.find_credential(connection.id, credential_id)
        if selected is None or not bool(getattr(selected, "enabled", False)):
            raise ValueError("credential not found or disabled")
    else:
        # Mirror ProviderRegistryRepository.resolve_credential ordering:
        # priority DESC, created_at ASC, id ASC, enabled rows only.
        enabled = [
            c for c in await repo.list_credentials(connection.id)
            if bool(getattr(c, "enabled", False))
        ]
        selected = min(enabled, key=_resolve_order_key) if enabled else None

    ciphertext = (
        selected.credential_encrypted
        if selected is not None
        else getattr(connection, "credential_encrypted", None)
    )
    credential = {"api_key": decrypt(ciphertext)} if ciphertext else {}
    driver_id = str(connection.driver or connection.template_id or "generic-openai")

    return DriverContext(
        connection_id=str(connection.id),
        credential_id=str(selected.id) if selected is not None else "",
        base_url=str(connection.base_url or ""),
        driver=driver_id,
        template_id=str(connection.template_id or ""),
        credential=credential,
        connection={"headers": dict(headers or {})},
        runtime=dict(runtime or {}),
    )
