import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DockerPackagingTests(unittest.TestCase):
    def test_gateway_image_includes_runtime_packages(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY apps ./apps", dockerfile)
        self.assertIn("COPY templates ./templates", dockerfile)
        self.assertIn("COPY router.py config.yaml ./", dockerfile)
        self.assertNotIn("aibox_catalog.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
