"""RED tests Step 131 — Release notes và CHANGELOG 1.0."""
from pathlib import Path
import unittest
import re

ROOT = Path(__file__).resolve().parents[2]

class ReleaseNotesTests(unittest.TestCase):
    def test_changelog_exists_and_has_version(self):
        p = ROOT / "CHANGELOG.md"
        self.assertTrue(p.exists())
        text = p.read_text(encoding="utf-8")
        self.assertIn("1.0", text)

    def test_release_notes_exist_and_covers_milestones(self):
        p = ROOT / "docs/release-notes/1.0.md"
        self.assertTrue(p.exists())
        text = p.read_text(encoding="utf-8", errors="ignore").lower()
        for keyword in ["control plane", "provider", "ledger", "quota", "scheduler", "compose"]:
            self.assertIn(keyword, text)

    def test_changelog_references_acceptance_criteria(self):
        p = ROOT / "CHANGELOG.md"
        text = p.read_text(encoding="utf-8")
        # should mention at least some AC numbers
        matches = re.findall(r"AC-\d+", text)
        assert len(matches) >= 5, f"Expected at least 5 AC references, got: {matches}"
