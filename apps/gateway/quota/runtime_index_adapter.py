"""Bridge quota authority reads into process-local RuntimeQuotaIndex."""
from __future__ import annotations

import inspect
from typing import Any, Iterable

from .runtime_index import RuntimeQuotaIndex


class RuntimeQuotaIndexAdapter:
    """Refresh local index from quota authority outside request ranking."""

    def __init__(self, index: RuntimeQuotaIndex, authority: Any) -> None:
        self.index = index
        self.authority = authority

    async def refresh_all(self) -> int:
        """Load complete authority snapshot; call only startup/background."""
        list_resources = getattr(self.authority, "list_resources", None)
        if not callable(list_resources):
            return 0
        resources = list_resources()
        if inspect.isawaitable(resources):
            resources = await resources
        resources = list(resources)
        self.index.replace_all(resources)
        return len(resources)

    async def refresh_ids(self, resource_ids: Iterable[str]) -> int:
        """Refresh explicitly named resources after a mutation."""
        snapshot = getattr(self.authority, "snapshot", None)
        if not callable(snapshot):
            return 0
        count = 0
        for resource_id in dict.fromkeys(resource_ids):
            try:
                resource = snapshot(resource_id)
                if inspect.isawaitable(resource):
                    resource = await resource
            except KeyError:
                continue
            self.index.apply_update(resource)
            count += 1
        return count
