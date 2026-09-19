import unittest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

class TestRouterNoProviderHardcode(unittest.TestCase):
    def test_router_no_provider_hardcode(self):
        router_path = Path("router.py")
        content = router_path.read_text(encoding="utf-8")
        
        # Check for banned substrings
        banned = ["aibox", "xkiro", "proxypal"]
        for term in banned:
            self.assertNotIn(
                term, 
                content.lower(), 
                f"Router core contains hardcoded provider reference: {term}"
            )

if __name__ == "__main__":
    unittest.main()
