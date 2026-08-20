import unittest

import router
from apps.worker.collectors import aibox_catalog


class CatalogImportCompatibilityTests(unittest.TestCase):
    def test_router_reexports_moved_catalog_helpers(self):
        self.assertIs(router.build_records, aibox_catalog.build_records)
        self.assertIs(router.select_routes, aibox_catalog.select_routes)
        self.assertIs(router.state_from_records, aibox_catalog.state_from_records)


if __name__ == "__main__":
    unittest.main()
