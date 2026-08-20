from typing import Type
from apps.gateway.providers.base import ProviderDriver, DriverNotFoundError

class DriverRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, Type[ProviderDriver]] = {}

    def register(self, driver_id: str, driver_class: Type[ProviderDriver]) -> None:
        self._registry[driver_id] = driver_class

    def resolve(self, driver_id: str) -> Type[ProviderDriver]:
        if driver_id not in self._registry:
            raise DriverNotFoundError(f"Driver not found: {driver_id}")
        return self._registry[driver_id]
