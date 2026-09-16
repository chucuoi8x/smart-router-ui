"""RED tests Step 130 — SLO and hardening docs."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]

class SLODocsTests(unittest.TestCase):
    def test_slo_doc_exists_and_covers_targets(self):
        p=ROOT/"docs/SLO.md"
        self.assertTrue(p.exists())
        text=p.read_text(encoding="utf-8").lower()
        for term in ["availability","p99","quota","secret","durability","alert"]:
            self.assertIn(term,text)

    def test_runbook_references_slo(self):
        p=ROOT/"docs/ops/runbook.md"
        self.assertIn("SLO",p.read_text(encoding="utf-8"))
