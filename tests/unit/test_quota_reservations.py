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

    def test_safety_buffer_reduces_effective_remaining_capacity(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                scope="tenant-a",
                metric="total_token",
                limit=100,
                window_seconds=60,
                safety_buffer=15,
            )
        )

        accepted = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=85,
            reservation_id="res_1",
        )
        rejected = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=1,
            reservation_id="res_2",
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.remaining, 0)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.remaining, 0)
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").remaining, 15)
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").effective_remaining, 0)

    def test_risk_buffer_is_reserved_and_released_during_reconciliation(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))

        reserved = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=60,
            reservation_id="res_1",
            risk_buffer=20,
        )
        reconciled = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 60})

        self.assertTrue(reserved.accepted)
        self.assertEqual(reserved.amount, 80)
        self.assertEqual(reserved.remaining, 20)
        self.assertEqual(reconciled.reserved_by_resource, {"tokens:tenant-a:gpt-4o:minute": 80})
        self.assertEqual(reconciled.released_by_resource, {"tokens:tenant-a:gpt-4o:minute": 20})
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

    def test_multi_resource_reservation_is_all_or_nothing(self):
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaReservationRequest,
            QuotaResource,
        )

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                scope="tenant-a",
                metric="total_token",
                limit=100,
                window_seconds=60,
            )
        )
        store.add_resource(
            QuotaResource(
                resource_id="requests:tenant-a:minute",
                scope="tenant-a",
                metric="request",
                limit=1,
                window_seconds=60,
            )
        )

        accepted = store.reserve_many(
            reservation_id="res_1",
            requests=[
                QuotaReservationRequest(resource_id="tokens:tenant-a:gpt-4o:minute", amount=60),
                QuotaReservationRequest(resource_id="requests:tenant-a:minute", amount=1),
            ],
        )
        rejected = store.reserve_many(
            reservation_id="res_2",
            requests=[
                QuotaReservationRequest(resource_id="tokens:tenant-a:gpt-4o:minute", amount=10),
                QuotaReservationRequest(resource_id="requests:tenant-a:minute", amount=1),
            ],
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(
            accepted.remaining_by_resource,
            {
                "tokens:tenant-a:gpt-4o:minute": 40,
                "requests:tenant-a:minute": 0,
            },
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "quota_exceeded")
        self.assertEqual(rejected.rejected_resource_id, "requests:tenant-a:minute")
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 60)
        self.assertEqual(store.snapshot("requests:tenant-a:minute").used, 1)

    def test_admission_check_rejects_when_hard_constraint_would_fail(self):
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaReservationRequest,
            QuotaResource,
        )

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60, used=95))

        result = store.check_many([QuotaReservationRequest("tokens:tenant-a:gpt-4o:minute", 10)])

        self.assertFalse(result.accepted)
        self.assertEqual(result.hard_failures, {"tokens:tenant-a:gpt-4o:minute": 5})
        self.assertEqual(result.soft_pressure_by_resource, {})
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 95)

    def test_admission_check_allows_soft_constraint_pressure_without_mutation(self):
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaReservationRequest,
            QuotaResource,
        )

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                "soft:tenant-a:daily-token-guidance",
                "tenant-a",
                "total_token",
                100,
                86400,
                used=95,
                hard_limit=False,
            )
        )

        result = store.check_many([QuotaReservationRequest("soft:tenant-a:daily-token-guidance", 10)])

        self.assertTrue(result.accepted)
        self.assertEqual(result.hard_failures, {})
        self.assertEqual(result.soft_pressure_by_resource, {"soft:tenant-a:daily-token-guidance": 5})
        self.assertEqual(store.snapshot("soft:tenant-a:daily-token-guidance").used, 95)

    def test_release_multi_resource_reservation_returns_all_capacity(self):
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaReservationRequest,
            QuotaResource,
        )

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.add_resource(QuotaResource("requests:tenant-a:minute", "tenant-a", "request", 2, 60))

        result = store.reserve_many(
            reservation_id="res_1",
            requests=[
                QuotaReservationRequest("tokens:tenant-a:gpt-4o:minute", 75),
                QuotaReservationRequest("requests:tenant-a:minute", 1),
            ],
        )
        released = store.release("res_1")

        self.assertTrue(result.accepted)
        self.assertTrue(released)
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 0)
        self.assertEqual(store.snapshot("requests:tenant-a:minute").used, 0)

    def test_multi_resource_reservation_uses_risk_buffer_per_constraint(self):
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaReservationRequest,
            QuotaResource,
        )

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.add_resource(QuotaResource("requests:tenant-a:minute", "tenant-a", "request", 2, 60))

        accepted = store.reserve_many(
            reservation_id="res_1",
            requests=[
                QuotaReservationRequest("tokens:tenant-a:gpt-4o:minute", 50, risk_buffer=10),
                QuotaReservationRequest("requests:tenant-a:minute", 1),
            ],
        )
        rejected = store.reserve_many(
            reservation_id="res_2",
            requests=[
                QuotaReservationRequest("tokens:tenant-a:gpt-4o:minute", 35, risk_buffer=6),
                QuotaReservationRequest("requests:tenant-a:minute", 1),
            ],
        )

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.remaining_by_resource["tokens:tenant-a:gpt-4o:minute"], 40)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.rejected_resource_id, "tokens:tenant-a:gpt-4o:minute")
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 60)
        self.assertEqual(store.snapshot("requests:tenant-a:minute").used, 1)

    def test_reconcile_releases_unused_reserved_capacity(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=80, reservation_id="res_1")

        result = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 50})
        repeated = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 10})

        self.assertEqual(result.reserved_by_resource, {"tokens:tenant-a:gpt-4o:minute": 80})
        self.assertEqual(result.actual_by_resource, {"tokens:tenant-a:gpt-4o:minute": 50})
        self.assertEqual(result.released_by_resource, {"tokens:tenant-a:gpt-4o:minute": 30})
        self.assertEqual(result.overshoot_by_resource, {})
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 50)
        self.assertEqual(repeated, result)
        self.assertFalse(store.release("res_1"))

    def test_reconcile_records_overshoot_when_actual_exceeds_reservation(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=60, reservation_id="res_1")

        result = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 75})

        self.assertEqual(result.released_by_resource, {})
        self.assertEqual(result.overshoot_by_resource, {"tokens:tenant-a:gpt-4o:minute": 15})
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 75)

    def test_reconcile_multi_resource_reservation_by_actual_usage(self):
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaReservationRequest,
            QuotaResource,
        )

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.add_resource(QuotaResource("requests:tenant-a:minute", "tenant-a", "request", 2, 60))
        store.reserve_many(
            reservation_id="res_1",
            requests=[
                QuotaReservationRequest("tokens:tenant-a:gpt-4o:minute", 70),
                QuotaReservationRequest("requests:tenant-a:minute", 1),
            ],
        )

        result = store.reconcile(
            "res_1",
            {
                "tokens:tenant-a:gpt-4o:minute": 55,
                "requests:tenant-a:minute": 1,
            },
        )

        self.assertEqual(result.released_by_resource, {"tokens:tenant-a:gpt-4o:minute": 15})
        self.assertEqual(result.overshoot_by_resource, {})
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 55)
        self.assertEqual(store.snapshot("requests:tenant-a:minute").used, 1)


if __name__ == "__main__":
    unittest.main()
