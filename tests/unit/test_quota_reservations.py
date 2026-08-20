import unittest
from concurrent.futures import ThreadPoolExecutor


class QuotaReservationTests(unittest.TestCase):
    def test_quota_resource_normalizes_scope_and_metric(self):
        from apps.gateway.quota.reservations import QuotaResource

        resource = QuotaResource(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="tokens",
            limit=100,
            window_seconds=60,
        )

        self.assertEqual(resource.scope, "tenant-a")
        self.assertEqual(resource.metric, "tokens")
        self.assertEqual(resource.limit, 100)
        self.assertEqual(resource.window_seconds, 60)
        self.assertEqual(resource.used, 0)
        self.assertEqual(resource.remaining, 100)

    def test_in_memory_reservation_rejects_amount_that_exceeds_hard_limit(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        resource = QuotaResource(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="tokens",
            limit=100,
            window_seconds=60,
        )
        store.add_resource(resource)

        accepted = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=60,
            reservation_id="res_1",
        )
        rejected = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=50,
            reservation_id="res_2",
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.remaining, 40)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "quota_exceeded")
        self.assertEqual(rejected.remaining, 40)
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 60)

    def test_release_returns_reserved_capacity(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                resource_id="requests:tenant-a:minute",
                scope="tenant-a",
                metric="requests",
                limit=2,
                window_seconds=60,
            )
        )

        first = store.reserve(resource_id="requests:tenant-a:minute", amount=1, reservation_id="res_1")
        store.release("res_1")
        second = store.reserve(resource_id="requests:tenant-a:minute", amount=2, reservation_id="res_2")

        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertEqual(second.remaining, 0)
        self.assertEqual(store.snapshot("requests:tenant-a:minute").used, 2)

    def test_concurrent_reservations_do_not_oversubscribe(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                scope="tenant-a",
                metric="tokens",
                limit=100,
                window_seconds=60,
            )
        )

        def reserve(index):
            return store.reserve(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                amount=30,
                reservation_id=f"res_{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))

        accepted = [result for result in results if result.accepted]
        rejected = [result for result in results if not result.accepted]
        self.assertEqual(len(accepted), 3)
        self.assertEqual(len(rejected), 5)
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 90)


if __name__ == "__main__":
    unittest.main()
