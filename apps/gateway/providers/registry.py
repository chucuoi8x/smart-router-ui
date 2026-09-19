from typing import Any, Type
from apps.gateway.providers.base import ProviderDriver, DriverNotFoundError
from apps.gateway.providers.driver_context import DriverContext

class DriverRegistry:
    def __init__(self) -> None:
        self._registry: dict[str, Type[ProviderDriver]] = {}

    def register(self, driver_id: str, driver_class: Type[ProviderDriver]) -> None:
        self._registry[driver_id] = driver_class

    def resolve(self, driver_id: str) -> Type[ProviderDriver]:
        if driver_id not in self._registry:
            raise DriverNotFoundError(f"Driver not found: {driver_id}")
        return self._registry[driver_id]

    def create(self, driver_id: str, context: DriverContext | dict[str, Any] | None = None) -> ProviderDriver:
        ctx = DriverContext.from_dict(context or {}) if isinstance(context, dict) else (context or DriverContext())
        driver_cls = self.resolve(driver_id)
        connection = ctx.connection if isinstance(ctx.connection, dict) else {}
        return driver_cls(base_url=ctx.base_url, headers=connection.get("headers"))


def default_driver_registry() -> DriverRegistry:
    """Return a DriverRegistry pre-registered with the built-in drivers
    under both their canonical IDs and protocol-alias keys."""
    from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver
    from apps.gateway.providers.generic_openai import GenericOpenAIDriver
    from apps.gateway.providers.generic_gemini import GenericGeminiDriver
    from apps.gateway.providers.cliproxy_bridge import CLIProxyBridgeDriver

    reg = DriverRegistry()
    # Canonical registrations
    reg.register(GenericAnthropicDriver.driver_id, GenericAnthropicDriver)
    reg.register(GenericOpenAIDriver.driver_id, GenericOpenAIDriver)
    reg.register(GenericGeminiDriver.driver_id, GenericGeminiDriver)
    reg.register(CLIProxyBridgeDriver.driver_id, CLIProxyBridgeDriver)
    # Protocol alias registrations
    reg.register("anthropic-compatible", GenericAnthropicDriver)
    reg.register("openai-compatible", GenericOpenAIDriver)
    reg.register("gemini-compatible", GenericGeminiDriver)
    reg.register("cliproxy-bridge", CLIProxyBridgeDriver)
    return reg

