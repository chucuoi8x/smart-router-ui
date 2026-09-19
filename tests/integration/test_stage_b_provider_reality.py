import pytest

from apps.gateway.providers.driver_context import DriverContext
from apps.gateway.providers.registry import default_driver_registry
from apps.gateway.providers.generic_openai import GenericOpenAIDriver


def test_registry_create_uses_common_driver_context():
    registry = default_driver_registry()
    context = DriverContext(base_url="https://provider.test/v1", credential={"api_key": "k"})
    driver = registry.create("generic-openai", context)
    assert isinstance(driver, GenericOpenAIDriver)
    assert driver.base_url == context.base_url
    assert driver._auth_headers(context)["authorization"] == "Bearer k"


def test_generic_drivers_delegate_real_http_execution():
    from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver
    from apps.gateway.providers.generic_gemini import GenericGeminiDriver
    assert GenericOpenAIDriver.delegates_request_execution is True
    assert GenericAnthropicDriver.delegates_request_execution is True
    assert GenericGeminiDriver.delegates_request_execution is True
