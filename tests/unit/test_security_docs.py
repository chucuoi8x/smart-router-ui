"""RED tests Step 132 — Security verification docs 1.0."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]

class SecurityDocsTests(unittest.TestCase):
    def test_security_doc_exists(self):
        self.assertTrue((ROOT / "docs/security.md").exists())

    def test_security_doc_covers_threat_model(self):
        text = (ROOT / "docs/security.md").read_text(encoding="utf-8", errors="ignore").lower()
        for term in ["threat", "credential", "encrypt", "audit", "auth", "secret"]:
            self.assertIn(term, text, f"missing: {term}")

    def test_security_doc_covers_verification(self):
        text = (ROOT / "docs/security.md").read_text(encoding="utf-8", errors="ignore").lower()
        self.assertIn("verification", text)
        self.assertIn("scan", text)
