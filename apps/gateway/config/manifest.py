import yaml
from typing import Dict, Any

class UnsafeManifestError(Exception):
    pass

class ManifestCompiler:
    def compile(self, manifest_yaml: str) -> Dict[str, Any]:
        # Perform strict checks for code injection patterns
        unsafe_patterns = [
            '__import__', 'eval(', 'exec(', 'subprocess', 'getattr', 'setattr',
            'delattr', 'pickle', 'marshal', 'shutil', 'builtins', 'globals()',
            'locals()', 'system('
        ]

        lower_yaml = manifest_yaml.lower()
        for pattern in unsafe_patterns:
            if pattern in lower_yaml:
                raise UnsafeManifestError(f'Manifest contains unsafe expression pattern: {pattern}')

        try:
            data = yaml.safe_load(manifest_yaml) or {}
        except yaml.YAMLError as e:
            raise UnsafeManifestError(f'YAML parsing error: {e}')

        if not isinstance(data, dict):
            raise UnsafeManifestError('Manifest must be a YAML mapping')

        return data
