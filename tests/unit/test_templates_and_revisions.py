import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

class TemplateAndRevisionTests(unittest.TestCase):
    def test_provider_template_registry_loads_templates(self):
        from apps.gateway.config.templates import ProviderTemplateRegistry

        registry = ProviderTemplateRegistry()
        # Verify we can list built-in templates
        templates = registry.list_templates()
        self.assertIn('openai', templates)
        self.assertIn('anthropic', templates)

        # Load a template
        openai_tmpl = registry.get_template('openai')
        self.assertEqual(openai_tmpl.get('driver'), 'generic-openai')

    def test_manifest_compiler_safety_boundary(self):
        from apps.gateway.config.manifest import ManifestCompiler, UnsafeManifestError

        compiler = ManifestCompiler()

        # Valid safe manifest
        safe_yaml = """
        id: custom-openai
        driver: openai-compatible
        transport:
          base_url: https://example.invalid/v1
        auth:
          type: bearer
        usage:
          input_tokens: usage.prompt_tokens
        """
        config = compiler.compile(safe_yaml)
        self.assertEqual(config['id'], 'custom-openai')

        # Invalid manifest with arbitrary code executable patterns
        unsafe_yaml = """
        id: custom-openai
        driver: openai-compatible
        transport:
          base_url: https://example.invalid/v1
        auth:
          type: bearer
        usage:
          input_tokens: "__import__('os').system('rm -rf /')"
        """
        with self.assertRaises(UnsafeManifestError):
            compiler.compile(unsafe_yaml)

    def test_config_revision_lifecycle(self):
        from apps.gateway.config.revision import ConfigRevisionManager
        from apps.gateway.config.snapshot import RuntimeConfigSnapshot

        manager = ConfigRevisionManager()

        # 1. Create a draft revision
        draft_id = manager.create_draft({"routes": {}, "connections": {}})
        self.assertTrue(draft_id.startswith('rev_'))

        # 2. Validate revision
        is_valid, errors = manager.validate(draft_id)
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

        # 3. Activate revision
        manager.activate(draft_id)
        active_rev = manager.get_active_revision()
        self.assertEqual(active_rev['revision_id'], draft_id)

        # 4. Resolve snapshot
        snapshot = manager.get_active_snapshot()
        self.assertIsInstance(snapshot, RuntimeConfigSnapshot)


if __name__ == '__main__':
    unittest.main()
