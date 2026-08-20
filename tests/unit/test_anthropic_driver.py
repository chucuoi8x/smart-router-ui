import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class AnthropicDriverTests(unittest.TestCase):
    def test_anthropic_driver_capabilities(self):
        from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver

        driver = GenericAnthropicDriver()
        caps = driver.capabilities()

        self.assertTrue(caps.get('supports_streaming'))
        self.assertEqual(driver.driver_id, 'generic-anthropic')

    def test_anthropic_driver_parses_usage(self):
        from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver

        driver = GenericAnthropicDriver()
        # Mock Anthropic API message response body
        response_body = {
            'id': 'msg_123',
            'type': 'message',
            'role': 'assistant',
            'content': [{'type': 'text', 'text': 'Hello'}],
            'model': 'claude-3-5-sonnet-20241022',
            'usage': {
                'input_tokens': 10,
                'output_tokens': 20
            }
        }

        usage = driver.parse_usage(response_body)
        self.assertEqual(usage.get('input_tokens'), 10)
        self.assertEqual(usage.get('output_tokens'), 20)
        self.assertEqual(usage.get('total_tokens'), 30)

    def test_anthropic_driver_classifies_errors(self):
        from apps.gateway.providers.generic_anthropic import GenericAnthropicDriver

        driver = GenericAnthropicDriver()

        # Test 429 Rate Limit classification
        err_429 = driver.classify_error(status_code=429, body={'error': {'type': 'rate_limit_error', 'message': 'Too many requests'}})
        self.assertEqual(err_429['kind'], 'RATE_LIMIT')
        self.assertTrue(err_429['retryable'])

        # Test 529 Overloaded classification
        err_529 = driver.classify_error(status_code=529, body={'error': {'type': 'overloaded_error', 'message': 'Overloaded'}})
        self.assertEqual(err_529['kind'], 'OVERLOADED')
        self.assertTrue(err_529['retryable'])

        # Test 401 Auth classification
        err_401 = driver.classify_error(status_code=401, body={'error': {'type': 'authentication_error', 'message': 'Invalid key'}})
        self.assertEqual(err_401['kind'], 'AUTH_EXPIRED')
        self.assertFalse(err_401['retryable'])


if __name__ == '__main__':
    unittest.main()
