"""Driver context and factory for provider drivers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DriverContext:
    """Context passed to driver methods.

    Wraps connection, credential, and runtime information.
    Backwards-compatible with plain dict ctx from admin.py.
    """

    connection_id: str = ""
    base_url: str = ""
    driver: str = ""
    template_id: str = ""
    credential: dict[str, Any] = field(default_factory=dict)
    connection: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, ctx: dict[str, Any] | DriverContext) -> DriverContext:
        """Convert plain dict ctx to DriverContext, or return as-is if already DriverContext."""
        if isinstance(ctx, cls):
            return ctx
        if isinstance(ctx, dict):
            return cls(
                connection_id=ctx.get("connection_id", ""),
                base_url=ctx.get("base_url", ""),
                driver=ctx.get("driver", ""),
                template_id=ctx.get("template_id", ""),
                credential=ctx.get("credential", {}),
                connection=ctx.get("connection", {}),
                runtime=ctx.get("runtime", {}),
            )
        raise TypeError(f"Expected dict or DriverContext, got {type(ctx)}")
