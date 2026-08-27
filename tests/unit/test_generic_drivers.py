import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class GenericDriverTests(unittest.TestCase):
    def test_openai_driver_capabilities_and_parsing(self):
        from apps.gateway.providers.generic_openai import GenericOpenAIDriver

        driver = GenericOpenAIDriver()
        caps = driver.capabilities()
        self.assertTrue(caps.get('supports_streaming'))
        self.assertEqual(driver.driver_id, 'generic-openai')

        # Test OpenAI usage parsing
        openai_res = {
            'id': 'chatcmpl-123',
            'object': 'chat.completion',
            'model': 'gpt-4o',
            'usage': {
                'prompt_tokens': 15,
                'completion_tokens': 25,
                'total_tokens': 40
            }
        }
        usage = driver.parse_usage(openai_res)
        self.assertEqual(usage['input_tokens'], 15)
        self.assertEqual(usage['output_tokens'], 25)
        self.assertEqual(usage['total_tokens'], 40)

    def test_gemini_driver_capabilities_and_parsing(self):
        from apps.gateway.providers.generic_gemini import GenericGeminiDriver

        driver = GenericGeminiDriver()
        caps = driver.capabilities()
        self.assertTrue(caps.get('supports_streaming'))
        self.assertEqual(driver.driver_id, 'generic-gemini')

        # Test Gemini usage parsing
        gemini_res = {
            'candidates': [{'content': {'parts': [{'text': 'Hello'}]}}],
            'usageMetadata': {
                'promptTokenCount': 12,
                'candidatesTokenCount': 18,
                'totalTokenCount': 30
            }
        }
        usage = driver.parse_usage(gemini_res)
        self.assertEqual(usage['input_tokens'], 12)
        self.assertEqual(usage['output_tokens'], 18)
        self.assertEqual(usage['total_tokens'], 30)

    def test_cliproxy_driver_capabilities(self):
        from apps.gateway.providers.cliproxy_bridge import CLIProxyBridgeDriver

        driver = CLIProxyBridgeDriver()
        caps = driver.capabilities()
        self.assertEqual(driver.driver_id, 'cliproxy-bridge')
        self.assertTrue(caps.get('supports_streaming'))

    def test_default_registry_resolves_protocol_aliases_and_concrete_ids(self):
        from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver
        from apps.gateway.providers.generic_openai import GenericOpenAIDriver
        from apps.gateway.providers.generic_gemini import GenericGeminiDriver
        from apps.gateway.providers.registry import default_driver_registry

        registry = default_driver_registry()

        self.assertIs(registry.resolve('anthropic-compatible'), GenericAnthropicDriver)
        self.assertIs(registry.resolve('generic-anthropic'), GenericAnthropicDriver)
        self.assertIs(registry.resolve('openai-compatible'), GenericOpenAIDriver)
        self.assertIs(registry.resolve('generic-openai'), GenericOpenAIDriver)
        self.assertIs(registry.resolve('gemini-compatible'), GenericGeminiDriver)
        self.assertIs(registry.resolve('generic-gemini'), GenericGeminiDriver)

    def test_generic_drivers_do_not_fabricate_exact_usage_when_metadata_absent(self):
        from apps.gateway.providers.generic_openai import GenericOpenAIDriver
        from apps.gateway.providers.generic_gemini import GenericGeminiDriver

        self.assertEqual({}, GenericOpenAIDriver().parse_usage({'id': 'chatcmpl-123'}))
        self.assertEqual({}, GenericGeminiDriver().parse_usage({'candidates': []}))


if __name__ == '__main__':
    unittest.main()
