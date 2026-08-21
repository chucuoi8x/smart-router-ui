import unittest
from concurrent.futures import ThreadPoolExecutor


class FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeQuotaSession:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.merged = []
        self.statements = []
        self.flushed = False
        self.committed = False

    async def merge(self, row):
        self.merged.append(row)
        self.rows[row.resource_id] = row
        return row

    async def get(self, model, key):
        return self.rows.get(key)

    async def scalars(self, statement):
        self.statements.append(statement)
        return FakeScalarResult(self.rows.values())

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


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
        self.assertEqual(resource.source, "configured")
        self.assertEqual(resource.confidence, "high")

    def test_quota_resource_maps_to_db_model(self):
        from apps.gateway.db.models import QuotaResourceState
        from apps.gateway.quota.reservations import QuotaResource

        resource = QuotaResource(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            window_seconds=60,
            used=25,
            safety_buffer=10,
            hard_limit=True,
            source="provider_api",
            confidence="exact",
            shared_group_id="account-weekly-123",
        )

        row = resource.to_db_model()

        self.assertIsInstance(row, QuotaResourceState)
        self.assertEqual(row.resource_id, "tokens:tenant-a:gpt-4o:minute")
        self.assertEqual(row.scope, "tenant-a")
        self.assertEqual(row.metric, "total_token")
        self.assertEqual(row.limit, 100)
        self.assertEqual(row.used, 25)
        self.assertEqual(row.window_seconds, 60)
        self.assertEqual(row.safety_buffer, 10)
        self.assertTrue(row.hard_limit)
        self.assertEqual(row.source, "provider_api")
        self.assertEqual(row.confidence, "exact")
        self.assertEqual(row.shared_group_id, "account-weekly-123")

    def test_quota_observation_updates_resource_with_provenance(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaObservation, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))

        updated = store.apply_observation(
            QuotaObservation(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                limit=120,
                used=25,
                source="provider_api",
                confidence="exact",
            )
        )

        self.assertEqual(updated.limit, 120)
        self.assertEqual(updated.used, 25)
        self.assertEqual(updated.source, "provider_api")
        self.assertEqual(updated.confidence, "exact")
        self.assertEqual(updated.scope, "tenant-a")
        self.assertEqual(updated.metric, "total_token")
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute"), updated)

    def test_quota_observation_can_update_safety_and_hard_limit(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaObservation, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                "tokens:tenant-a:gpt-4o:minute",
                "tenant-a",
                "total_token",
                100,
                60,
                safety_buffer=10,
            )
        )

        updated = store.apply_observation(
            QuotaObservation(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                limit=100,
                used=50,
                source="response_header",
                confidence="high",
                safety_buffer=20,
                hard_limit=False,
            )
        )

        self.assertEqual(updated.safety_buffer, 20)
        self.assertFalse(updated.hard_limit)
        self.assertEqual(updated.effective_remaining, 30)

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

    def test_single_reservation_id_replay_requires_same_request_shape(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))

        first = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=10,
            reservation_id="res_1",
            risk_buffer=5,
        )
        repeated = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=10,
            reservation_id="res_1",
            risk_buffer=5,
        )

        self.assertEqual(repeated, first)
        with self.assertRaisesRegex(ValueError, "reservation_id conflict"):
            store.reserve(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                amount=15,
                reservation_id="res_1",
                risk_buffer=0,
            )
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 15)

    def test_rejected_reservation_id_replay_requires_same_request_shape(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 10, 60))

        first = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=15,
            reservation_id="res_1",
        )
        repeated = store.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=15,
            reservation_id="res_1",
        )

        self.assertFalse(first.accepted)
        self.assertEqual(repeated, first)
        with self.assertRaisesRegex(ValueError, "reservation_id conflict"):
            store.reserve(
                resource_id="tokens:tenant-a:gpt-4o:minute",
                amount=11,
                reservation_id="res_1",
            )

    def test_reservation_id_conflicts_across_single_and_batch_forms(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaReservationRequest, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))

        store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=10, reservation_id="res_1")

        with self.assertRaisesRegex(ValueError, "reservation_id conflict"):
            store.reserve_many(
                reservation_id="res_1",
                requests=[QuotaReservationRequest("tokens:tenant-a:gpt-4o:minute", 10)],
            )

    def test_reservation_id_cannot_be_reused_after_reconciliation(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=10, reservation_id="res_1")
        store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 10})

        with self.assertRaisesRegex(ValueError, "reservation_id conflict"):
            store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=10, reservation_id="res_1")
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 10)

    def test_rejected_reservation_id_cannot_be_reused_after_release_cleanup(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 10, 60))
        rejected = store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=15, reservation_id="res_1")
        released = store.release("res_1")

        self.assertFalse(rejected.accepted)
        self.assertFalse(released)
        with self.assertRaisesRegex(ValueError, "reservation_id conflict"):
            store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=1, reservation_id="res_1")
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 0)

    def test_batch_reservation_id_replay_requires_same_request_tuple(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaReservationRequest, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.add_resource(QuotaResource("requests:tenant-a:minute", "tenant-a", "request", 10, 60))
        requests = [
            QuotaReservationRequest("tokens:tenant-a:gpt-4o:minute", 10, risk_buffer=5),
            QuotaReservationRequest("requests:tenant-a:minute", 1),
        ]

        first = store.reserve_many(reservation_id="res_1", requests=requests)
        repeated = store.reserve_many(reservation_id="res_1", requests=list(requests))

        self.assertEqual(repeated, first)
        with self.assertRaisesRegex(ValueError, "reservation_id conflict"):
            store.reserve_many(reservation_id="res_1", requests=list(reversed(requests)))
        with self.assertRaisesRegex(ValueError, "reservation_id conflict"):
            store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=15, reservation_id="res_1")
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 15)
        self.assertEqual(store.snapshot("requests:tenant-a:minute").used, 1)

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

    def test_shared_quota_group_reservation_consumes_shared_capacity(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                "model-a:account-weekly",
                "tenant-a",
                "total_token",
                100,
                604800,
                shared_group_id="account-weekly-123",
            )
        )
        store.add_resource(
            QuotaResource(
                "model-b:account-weekly",
                "tenant-a",
                "total_token",
                100,
                604800,
                shared_group_id="account-weekly-123",
            )
        )

        accepted = store.reserve(resource_id="model-a:account-weekly", amount=70, reservation_id="res_1")
        rejected = store.reserve(resource_id="model-b:account-weekly", amount=40, reservation_id="res_2")

        self.assertTrue(accepted.accepted)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.remaining, 30)
        self.assertEqual(store.snapshot("model-a:account-weekly").used, 70)
        self.assertEqual(store.snapshot("model-b:account-weekly").used, 70)
        self.assertEqual(store.snapshot("model-b:account-weekly").effective_remaining, 30)

    def test_shared_quota_group_admission_projects_usage_without_mutation(self):
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaReservationRequest,
            QuotaResource,
        )

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource("model-a:account-weekly", "tenant-a", "total_token", 100, 604800, shared_group_id="account-weekly-123")
        )
        store.add_resource(
            QuotaResource("model-b:account-weekly", "tenant-a", "total_token", 100, 604800, shared_group_id="account-weekly-123")
        )

        result = store.check_many(
            [
                QuotaReservationRequest("model-a:account-weekly", 60),
                QuotaReservationRequest("model-b:account-weekly", 50),
            ]
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.hard_failures, {"model-b:account-weekly": 10})
        self.assertEqual(store.snapshot("model-a:account-weekly").used, 0)
        self.assertEqual(store.snapshot("model-b:account-weekly").used, 0)

    def test_shared_quota_group_observation_updates_peer_usage(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaObservation, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource("model-a:account-weekly", "tenant-a", "total_token", 100, 604800, shared_group_id="account-weekly-123")
        )
        store.add_resource(
            QuotaResource("model-b:account-weekly", "tenant-a", "total_token", 100, 604800, shared_group_id="account-weekly-123")
        )

        updated = store.apply_observation(
            QuotaObservation(
                resource_id="model-a:account-weekly",
                limit=100,
                used=65,
                source="provider_api",
                confidence="exact",
            )
        )
        rejected = store.reserve(
            resource_id="model-b:account-weekly",
            amount=36,
            reservation_id="res_1",
        )

        self.assertEqual(updated.used, 65)
        self.assertEqual(store.snapshot("model-a:account-weekly").used, 65)
        self.assertEqual(store.snapshot("model-b:account-weekly").used, 65)
        self.assertEqual(store.snapshot("model-b:account-weekly").source, "configured")
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.remaining, 35)

    def test_shared_quota_group_observation_updates_peer_capacity(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaObservation, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(
            QuotaResource(
                "model-a:account-weekly",
                "tenant-a",
                "total_token",
                100,
                604800,
                safety_buffer=5,
                shared_group_id="account-weekly-123",
            )
        )
        store.add_resource(
            QuotaResource(
                "model-b:account-weekly",
                "tenant-a",
                "total_token",
                100,
                604800,
                safety_buffer=5,
                shared_group_id="account-weekly-123",
            )
        )

        store.apply_observation(
            QuotaObservation(
                resource_id="model-a:account-weekly",
                limit=120,
                used=40,
                source="provider_api",
                confidence="exact",
                safety_buffer=10,
            )
        )

        peer = store.snapshot("model-b:account-weekly")
        self.assertEqual(peer.limit, 120)
        self.assertEqual(peer.used, 40)
        self.assertEqual(peer.safety_buffer, 10)
        self.assertEqual(peer.effective_remaining, 70)
        self.assertEqual(peer.source, "configured")
        self.assertEqual(peer.confidence, "high")

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

    def test_reconcile_requires_actual_usage_for_every_reserved_resource(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=80, reservation_id="res_1")

        with self.assertRaisesRegex(ValueError, "actual usage missing resource_id: tokens:tenant-a:gpt-4o:minute"):
            store.reconcile("res_1", {})

        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 80)
        result = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 70})
        self.assertEqual(result.actual_by_resource, {"tokens:tenant-a:gpt-4o:minute": 70})

    def test_reconcile_rejects_unexpected_actual_usage_resource(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=80, reservation_id="res_1")

        with self.assertRaisesRegex(ValueError, "actual usage contains unknown resource_id: requests:tenant-a:minute"):
            store.reconcile(
                "res_1",
                {
                    "tokens:tenant-a:gpt-4o:minute": 70,
                    "requests:tenant-a:minute": 1,
                },
            )

        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 80)
        result = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 70})
        self.assertEqual(result.actual_by_resource, {"tokens:tenant-a:gpt-4o:minute": 70})

    def test_reconcile_multi_resource_requires_exact_actual_usage_keys(self):
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

        with self.assertRaisesRegex(ValueError, "actual usage missing resource_id: requests:tenant-a:minute"):
            store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 55})

        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 70)
        self.assertEqual(store.snapshot("requests:tenant-a:minute").used, 1)
        result = store.reconcile(
            "res_1",
            {
                "tokens:tenant-a:gpt-4o:minute": 55,
                "requests:tenant-a:minute": 1,
            },
        )
        self.assertEqual(result.actual_by_resource, {"tokens:tenant-a:gpt-4o:minute": 55, "requests:tenant-a:minute": 1})

    def test_repeated_reconcile_returns_original_result_before_validating_input(self):
        from apps.gateway.quota.reservations import InMemoryQuotaReservations, QuotaResource

        store = InMemoryQuotaReservations()
        store.add_resource(QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60))
        store.reserve(resource_id="tokens:tenant-a:gpt-4o:minute", amount=80, reservation_id="res_1")

        result = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": 70})
        repeated_unknown = store.reconcile("res_1", {"unexpected:resource": 1})
        repeated_negative = store.reconcile("res_1", {"tokens:tenant-a:gpt-4o:minute": -1})

        self.assertEqual(repeated_unknown, result)
        self.assertEqual(repeated_negative, result)
        self.assertEqual(store.snapshot("tokens:tenant-a:gpt-4o:minute").used, 70)


class QuotaResourceRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_saves_resource_state_with_flush_only_by_default(self):
        from apps.gateway.db.models import QuotaResourceState
        from apps.gateway.quota.reservations import QuotaResource, QuotaResourceRepository

        session = FakeQuotaSession()
        repository = QuotaResourceRepository(session)
        resource = QuotaResource(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            window_seconds=60,
            used=25,
            safety_buffer=10,
            source="provider_api",
            confidence="exact",
        )

        row = await repository.save_resource(resource)

        self.assertIsInstance(row, QuotaResourceState)
        self.assertEqual(session.merged, [row])
        self.assertTrue(session.flushed)
        self.assertFalse(session.committed)
        self.assertEqual(row.resource_id, "tokens:tenant-a:gpt-4o:minute")
        self.assertEqual(row.used, 25)
        self.assertEqual(row.source, "provider_api")
        self.assertEqual(row.confidence, "exact")

    async def test_repository_can_commit_saved_resource(self):
        from apps.gateway.quota.reservations import QuotaResource, QuotaResourceRepository

        session = FakeQuotaSession()
        repository = QuotaResourceRepository(session)
        resource = QuotaResource("tokens:tenant-a:gpt-4o:minute", "tenant-a", "total_token", 100, 60)

        await repository.save_resource(resource, commit=True)

        self.assertTrue(session.flushed)
        self.assertTrue(session.committed)

    async def test_repository_gets_resource_by_id_as_domain_object(self):
        from apps.gateway.db.models import QuotaResourceState
        from apps.gateway.quota.reservations import QuotaResourceRepository

        row = QuotaResourceState(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=25,
            window_seconds=60,
            safety_buffer=10,
            hard_limit=False,
            source="provider_api",
            confidence="exact",
            shared_group_id="account-weekly-123",
        )
        session = FakeQuotaSession(rows={row.resource_id: row})
        repository = QuotaResourceRepository(session)

        resource = await repository.get_resource("tokens:tenant-a:gpt-4o:minute")
        missing = await repository.get_resource("missing")

        self.assertIsNotNone(resource)
        assert resource is not None
        self.assertEqual(resource.resource_id, "tokens:tenant-a:gpt-4o:minute")
        self.assertEqual(resource.scope, "tenant-a")
        self.assertEqual(resource.metric, "total_token")
        self.assertEqual(resource.limit, 100)
        self.assertEqual(resource.used, 25)
        self.assertEqual(resource.window_seconds, 60)
        self.assertEqual(resource.safety_buffer, 10)
        self.assertFalse(resource.hard_limit)
        self.assertEqual(resource.source, "provider_api")
        self.assertEqual(resource.confidence, "exact")
        self.assertEqual(resource.shared_group_id, "account-weekly-123")
        self.assertIsNone(missing)

    async def test_repository_lists_resources_as_domain_objects(self):
        from apps.gateway.db.models import QuotaResourceState
        from apps.gateway.quota.reservations import QuotaResourceRepository

        token_row = QuotaResourceState(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=25,
            window_seconds=60,
            source="provider_api",
            confidence="exact",
        )
        request_row = QuotaResourceState(
            resource_id="requests:tenant-a:minute",
            scope="tenant-a",
            metric="request",
            limit=10,
            used=2,
            window_seconds=60,
        )
        session = FakeQuotaSession(
            rows={
                token_row.resource_id: token_row,
                request_row.resource_id: request_row,
            }
        )
        repository = QuotaResourceRepository(session)

        resources = await repository.list_resources()

        self.assertEqual([resource.resource_id for resource in resources], [token_row.resource_id, request_row.resource_id])
        self.assertEqual(resources[0].source, "provider_api")
        self.assertEqual(resources[0].confidence, "exact")
        self.assertEqual(resources[1].metric, "request")
        self.assertEqual(len(session.statements), 1)

    async def test_hydrate_reservations_from_repository(self):
        from apps.gateway.db.models import QuotaResourceState
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaResourceRepository,
        )

        row1 = QuotaResourceState(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=25,
            window_seconds=60,
            safety_buffer=10,
            hard_limit=True,
            source="provider_api",
            confidence="exact",
        )
        row2 = QuotaResourceState(
            resource_id="requests:tenant-a:minute",
            scope="tenant-a",
            metric="request",
            limit=10,
            used=2,
            window_seconds=60,
        )
        # Two resources sharing a group
        row3 = QuotaResourceState(
            resource_id="model-a:account-weekly",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=30,
            window_seconds=604800,
            shared_group_id="account-weekly-123",
        )
        row4 = QuotaResourceState(
            resource_id="model-b:account-weekly",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=30,
            window_seconds=604800,
            shared_group_id="account-weekly-123",
        )

        session = FakeQuotaSession(
            rows={
                row1.resource_id: row1,
                row2.resource_id: row2,
                row3.resource_id: row3,
                row4.resource_id: row4,
            }
        )
        repository = QuotaResourceRepository(session)
        reservations = await InMemoryQuotaReservations.from_repository(repository)

        # Verify individual resources were loaded
        self.assertEqual(reservations.snapshot("tokens:tenant-a:gpt-4o:minute").used, 25)
        self.assertEqual(reservations.snapshot("tokens:tenant-a:gpt-4o:minute").safety_buffer, 10)
        self.assertEqual(reservations.snapshot("tokens:tenant-a:gpt-4o:minute").source, "provider_api")
        self.assertEqual(reservations.snapshot("requests:tenant-a:minute").used, 2)

        # Verify shared group synchronization: both have the same used value
        self.assertEqual(reservations.snapshot("model-a:account-weekly").used, 30)
        self.assertEqual(reservations.snapshot("model-b:account-weekly").used, 30)

        # Verify capacity checks work after hydration: requesting 70 exceeds effective remaining (65)
        rejected = reservations.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=70,
            reservation_id="res_1",
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "quota_exceeded")

        # Requesting 60 fits within effective remaining (65)
        accepted2 = reservations.reserve(
            resource_id="tokens:tenant-a:gpt-4o:minute",
            amount=60,
            reservation_id="res_2",
        )
        self.assertTrue(accepted2.accepted)
        self.assertEqual(accepted2.remaining, 5)  # 100 - 25 - 60 - 10 = 5

        # Check shared group reservation after hydration
        shared_accepted = reservations.reserve(
            resource_id="model-a:account-weekly",
            amount=60,
            reservation_id="res_3",
        )
        self.assertTrue(shared_accepted.accepted)
        self.assertEqual(shared_accepted.remaining, 10)  # 100 - 30 - 60 = 10
        # model-b should reflect the same used value
        self.assertEqual(reservations.snapshot("model-b:account-weekly").used, 90)

    async def test_hydrate_shared_group_uses_conservative_persisted_usage(self):
        from apps.gateway.db.models import QuotaResourceState
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaResourceRepository,
        )

        row1 = QuotaResourceState(
            resource_id="model-a:account-weekly",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=30,
            window_seconds=604800,
            shared_group_id="account-weekly-123",
        )
        row2 = QuotaResourceState(
            resource_id="model-b:account-weekly",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=45,
            window_seconds=604800,
            shared_group_id="account-weekly-123",
        )
        session = FakeQuotaSession(rows={row1.resource_id: row1, row2.resource_id: row2})
        repository = QuotaResourceRepository(session)

        reservations = await InMemoryQuotaReservations.from_repository(repository)

        self.assertEqual(reservations.snapshot("model-a:account-weekly").used, 45)
        self.assertEqual(reservations.snapshot("model-b:account-weekly").used, 45)
        accepted = reservations.reserve(
            resource_id="model-a:account-weekly",
            amount=55,
            reservation_id="res_1",
        )
        rejected = reservations.reserve(
            resource_id="model-b:account-weekly",
            amount=1,
            reservation_id="res_2",
        )
        self.assertTrue(accepted.accepted)
        self.assertFalse(rejected.accepted)

    async def test_hydrate_shared_group_uses_conservative_capacity_fields(self):
        from apps.gateway.db.models import QuotaResourceState
        from apps.gateway.quota.reservations import (
            InMemoryQuotaReservations,
            QuotaResourceRepository,
        )

        row1 = QuotaResourceState(
            resource_id="model-a:account-weekly",
            scope="tenant-a",
            metric="total_token",
            limit=100,
            used=80,
            window_seconds=604800,
            safety_buffer=5,
            hard_limit=False,
            shared_group_id="account-weekly-123",
        )
        row2 = QuotaResourceState(
            resource_id="model-b:account-weekly",
            scope="tenant-a",
            metric="total_token",
            limit=60,
            used=50,
            window_seconds=604800,
            safety_buffer=10,
            hard_limit=True,
            shared_group_id="account-weekly-123",
        )
        session = FakeQuotaSession(rows={row1.resource_id: row1, row2.resource_id: row2})
        repository = QuotaResourceRepository(session)

        reservations = await InMemoryQuotaReservations.from_repository(repository)

        resource_a = reservations.snapshot("model-a:account-weekly")
        resource_b = reservations.snapshot("model-b:account-weekly")
        self.assertEqual(resource_a.limit, 80)
        self.assertEqual(resource_b.limit, 80)
        self.assertEqual(resource_a.used, 80)
        self.assertEqual(resource_b.used, 80)
        self.assertEqual(resource_a.safety_buffer, 10)
        self.assertEqual(resource_b.safety_buffer, 10)
        self.assertTrue(resource_a.hard_limit)
        self.assertTrue(resource_b.hard_limit)
        self.assertEqual(resource_a.effective_remaining, 0)
        self.assertEqual(resource_b.effective_remaining, 0)

if __name__ == "__main__":
    unittest.main()
