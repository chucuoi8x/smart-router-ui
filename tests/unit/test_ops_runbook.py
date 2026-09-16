"""RED tests Step 123 — ops runbook cho acceptance 1.0."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/ops/runbook.md"
BACKUP = ROOT / "docs/ops/backup-restore.md"

class RunbookTests(unittest.TestCase):
    def test_runbook_exists(self):
        self.assertTrue(RUNBOOK.exists(), f"Runbook not found at {RUNBOOK}")

    def test_runbook_covers_required_sections(self):
        self.assertTrue(RUNBOOK.exists())
        text = RUNBOOK.read_text(encoding="utf-8", errors="ignore")
        for keyword in ["compose", "migration", "overview", "health", "ledger", "audit", "acceptance", "troubleshoot"]:
            self.assertIn(keyword.lower(), text.lower(), f"Runbook missing coverage for: {keyword}")

    def test_backup_restore_still_documented(self):
        self.assertTrue(BACKUP.exists())
