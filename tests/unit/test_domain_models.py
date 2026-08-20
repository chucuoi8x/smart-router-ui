import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class ResourceRefTests(unittest.TestCase):
    def test_resource_ref_holds_connection_credential_model(self):
        from apps.gateway.routing.models import ResourceRef

        ref = ResourceRef(
            provider_connection_id='conn-123',
            credential_scope='cred-456',
            model_id='gpt-4'
        )

        self.assertEqual(ref.provider_connection_id, 'conn-123')
        self.assertEqual(ref.credential_scope, 'cred-456')
        self.assertEqual(ref.model_id, 'gpt-4')

    def test_resource_ref_equality_uses_all_fields(self):
        from apps.gateway.routing.models import ResourceRef

        ref1 = ResourceRef('conn-1', 'cred-1', 'model-a')
        ref2 = ResourceRef('conn-1', 'cred-1', 'model-a')
        ref3 = ResourceRef('conn-1', 'cred-2', 'model-a')

        self.assertEqual(ref1, ref2)
        self.assertNotEqual(ref1, ref3)

    def test_resource_ref_hashable(self):
        from apps.gateway.routing.models import ResourceRef

        ref = ResourceRef('conn-1', 'cred-1', 'model-a')
        s = {ref, ref}
        self.assertEqual(len(s), 1)


class ResourceCandidateTests(unittest.TestCase):
    def test_candidate_has_resource_ref_driver_and_weight(self):
        from apps.gateway.routing.models import ResourceRef, ResourceCandidate

        ref = ResourceRef('conn-1', 'cred-1', 'model-a')
        candidate = ResourceCandidate(
            resource_ref=ref,
            driver_id='openai-compatible',
            weight=5,
            metadata={'latency_p50': 120}
        )

        self.assertEqual(candidate.resource_ref, ref)
        self.assertEqual(candidate.driver_id, 'openai-compatible')
        self.assertEqual(candidate.weight, 5)
        self.assertEqual(candidate.metadata, {'latency_p50': 120})

    def test_candidate_default_weight_is_one(self):
        from apps.gateway.routing.models import ResourceRef, ResourceCandidate

        ref = ResourceRef('conn-1', 'cred-1', 'model-a')
        candidate = ResourceCandidate(ref, 'openai-compatible')

        self.assertEqual(candidate.weight, 1)
        self.assertEqual(candidate.metadata, {})


class CapabilityTests(unittest.TestCase):
    def test_capability_holds_value_source_confidence(self):
        from apps.gateway.routing.models import Capability

        cap = Capability(
            value=128000,
            source='template',
            confidence='high'
        )

        self.assertEqual(cap.value, 128000)
        self.assertEqual(cap.source, 'template')
        self.assertEqual(cap.confidence, 'high')

    def test_capability_with_unknown_confidence(self):
        from apps.gateway.routing.models import Capability

        cap = Capability(value=True, source='inferred', confidence='low')
        self.assertEqual(cap.confidence, 'low')


class DriverRegistryTests(unittest.TestCase):
    def test_register_and_resolve_driver(self):
        from apps.gateway.providers.registry import DriverRegistry
        from apps.gateway.providers.base import ProviderDriver

        class DummyDriver(ProviderDriver):
            async def validate_connection(self, ctx):
                pass
            async def discover_models(self, ctx):
                return []
            async def execute(self, ctx, request):
                pass
            async def execute_stream(self, ctx, request):
                async def gen():
                    yield b''
                return gen()
            async def fetch_quota(self, ctx):
                return []
            def parse_usage(self, response):
                return {}
            def classify_error(self, error_or_response):
                return {'kind': 'UNKNOWN', 'retryable': False}
            def capabilities(self):
                return {'supports_streaming': True}

        registry = DriverRegistry()
        registry.register('dummy', DummyDriver)

        resolved = registry.resolve('dummy')
        self.assertEqual(resolved, DummyDriver)

    def test_resolve_unknown_driver_raises(self):
        from apps.gateway.providers.registry import DriverRegistry
        from apps.gateway.providers.base import DriverNotFoundError

        registry = DriverRegistry()
        with self.assertRaises(DriverNotFoundError):
            registry.resolve('unknown')

    def test_register_overwrites_existing(self):
        from apps.gateway.providers.registry import DriverRegistry
        from apps.gateway.providers.base import ProviderDriver

        class DriverA(ProviderDriver):
            async def validate_connection(self, ctx): pass
            async def discover_models(self, ctx): return []
            async def execute(self, ctx, request): pass
            async def execute_stream(self, ctx, request):
                async def gen():
                    yield b''
                return gen()
            async def fetch_quota(self, ctx): return []
            def parse_usage(self, response): return {}
            def classify_error(self, error_or_response): return {'kind': 'UNKNOWN', 'retryable': False}
            def capabilities(self): return {}

        class DriverB(ProviderDriver):
            async def validate_connection(self, ctx): pass
            async def discover_models(self, ctx): return []
            async def execute(self, ctx, request): pass
            async def execute_stream(self, ctx, request):
                async def gen():
                    yield b''
                return gen()
            async def fetch_quota(self, ctx): return []
            def parse_usage(self, response): return {}
            def classify_error(self, error_or_response): return {'kind': 'UNKNOWN', 'retryable': False}
            def capabilities(self): return {}

        registry = DriverRegistry()
        registry.register('test', DriverA)
        registry.register('test', DriverB)
        resolved = registry.resolve('test')
        self.assertEqual(resolved, DriverB)


if __name__ == '__main__':
    unittest.main()
