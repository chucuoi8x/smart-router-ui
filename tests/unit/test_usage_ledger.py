import asyncio
import unittest


class FakeAsyncSession:
    def __init__(self):
        self.added = []
        self.committed = False
        self.flushed = False

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


class UsageLedgerTests(unittest.TestCase):
    def test_usage_event_normalizes_provider_usage(self):
        from apps.gateway.usage.ledger import UsageEvent

        event = UsageEvent.from_parsed_usage(
            request_id="req_1",
            attempt_id="att_1",
            provider_connection_id="conn_1",
            credential_id="cred_1",
            model_resource_id="model_1",
            usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "cached_input_tokens": 3,
                "source": "provider_api",
                "confidence": "exact",
                "estimated": False,
            },
        )

        self.assertEqual(event.input_tokens, 10)
        self.assertEqual(event.output_tokens, 20)
        self.assertEqual(event.cached_input_tokens, 3)
        self.assertEqual(event.total_tokens, 30)
        self.assertEqual(event.source, "provider_api")
        self.assertEqual(event.confidence, "exact")
        self.assertFalse(event.estimated)

    def test_estimated_usage_never_becomes_exact_provider_truth(self):
        from apps.gateway.usage.ledger import UsageEvent

        event = UsageEvent.from_parsed_usage(
            request_id="req_estimated",
            attempt_id="att_estimated",
            provider_connection_id="conn_1",
            credential_id=None,
            model_resource_id="model_1",
            usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "source": "local_estimate",
                "confidence": "estimated",
            },
        )

        self.assertEqual(event.source, "local_estimate")
        self.assertEqual(event.confidence, "estimated")
        self.assertTrue(event.estimated)

    def test_manager_api_failure_does_not_break_data_plane(self):
        """When ledger.record_request fails with exception, routing must still return."""
        import asyncio

        from apps.gateway.usage.ledger import InMemoryUsageLedger
        from router import SmartRouter

        class BrokenLedger(InMemoryUsageLedger):
            def __init__(self):
                super().__init__()
                self.call_count = 0
            def record_request(self, *args, **kwargs):
                self.call_count += 1
                raise RuntimeError("DB connection failed")
            def record_attempt(self, *args, **kwargs):
                self.call_count += 1
                raise RuntimeError("DB connection failed")
            def record_usage(self, *args, **kwargs):
                self.call_count += 1
                raise RuntimeError("DB connection failed")

        broken = BrokenLedger()

        router = SmartRouter({
            "routes": {"route-ok": {"strategy": "priority", "candidates": [{"upstream": "a", "model": "m"}]}},
            "logging": {"level": "CRITICAL"},
        })
        # Inject the broken ledger as the fallback (used when no per-request contextvar set)
        router._usage_ledger = broken

        # Directly call the private methods that use ledger — these should swallow exceptions
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(router._record_usage_request(request_id="req-x", route_name="route-ok"))
            from router import Candidate
            cand = Candidate("a", "m")
            loop.run_until_complete(
                router._record_usage_attempt(
                    request_id="req-x", attempt_id="att-1", candidate=cand, status="success"
                )
            )
        except Exception as exc:
            self.fail(f"Ledger failure propagated to caller: {exc}")
        finally:
            loop.close()

        # Ledger path must have been exercised
        self.assertGreater(broken.call_count, 0, "ledger must be attempted before swallowing failure")

    def test_request_totals_include_all_attempts(self):
        from apps.gateway.usage.ledger import InMemoryUsageLedger, UsageEvent

        ledger = InMemoryUsageLedger()
        ledger.record_request(request_id="req_1", route_id="chat", logical_model="smart-chat")
        ledger.record_attempt(
            request_id="req_1",
            attempt_id="att_1",
            provider_connection_id="conn_a",
            model_resource_id="claude",
            status="RATE_LIMIT",
        )
        ledger.record_usage(
            UsageEvent.from_parsed_usage(
                request_id="req_1",
                attempt_id="att_1",
                provider_connection_id="conn_a",
                credential_id=None,
                model_resource_id="claude",
                usage={"input_tokens": 5, "output_tokens": 0, "source": "provider_api", "confidence": "exact"},
            )
        )
        ledger.record_attempt(
            request_id="req_1",
            attempt_id="att_2",
            provider_connection_id="conn_b",
            model_resource_id="gpt-4o",
            status="success",
        )
        ledger.record_usage(
            UsageEvent.from_parsed_usage(
                request_id="req_1",
                attempt_id="att_2",
                provider_connection_id="conn_b",
                credential_id="cred_b",
                model_resource_id="gpt-4o",
                usage={"input_tokens": 10, "output_tokens": 20, "source": "provider_api", "confidence": "exact"},
            )
        )

        totals = ledger.request_totals("req_1")
        self.assertEqual(totals["input_tokens"], 15)
        self.assertEqual(totals["output_tokens"], 20)
        self.assertEqual(totals["total_tokens"], 35)
        self.assertEqual(totals["attempt_count"], 2)
        self.assertEqual(totals["usage_event_count"], 2)

    def test_request_metadata_does_not_store_raw_payload_or_secrets(self):
        from apps.gateway.usage.ledger import InMemoryUsageLedger

        ledger = InMemoryUsageLedger()
        record = ledger.record_request(
            request_id="req_1",
            route_id="chat",
            logical_model="smart-chat",
            metadata={
                "tenant": "local-dev",
                "raw_prompt": "do not persist",
                "raw_response": "do not persist",
                "authorization": "Bearer secret-token",
                "provider_secret": "sk-secret",
                "cookie": "session=secret",
            },
        )

        exported = record.to_public_dict()
        self.assertEqual(exported["metadata"], {"tenant": "local-dev"})
        self.assertNotIn("raw_prompt", exported["metadata"])
        self.assertNotIn("raw_response", exported["metadata"])
        self.assertNotIn("authorization", exported["metadata"])
        self.assertNotIn("provider_secret", exported["metadata"])
        self.assertNotIn("cookie", exported["metadata"])

    def test_request_and_attempt_records_map_to_db_models(self):
        from apps.gateway.db.models import RequestLedger, AttemptLedger
        from apps.gateway.usage.ledger import AttemptRecord, RequestRecord

        request = RequestRecord(
            request_id="req_1",
            route_id="chat",
            logical_model="smart-chat",
            metadata={"tenant": "local-dev"},
        )
        request_row = request.to_db_model()
        self.assertIsInstance(request_row, RequestLedger)
        self.assertEqual(request_row.request_id, "req_1")
        self.assertEqual(request_row.route_id, "chat")
        self.assertEqual(request_row.logical_model, "smart-chat")
        self.assertEqual(request_row.metadata_, {"tenant": "local-dev"})

        attempt = AttemptRecord(
            request_id="req_1",
            attempt_id="att_1",
            provider_connection_id="conn_1",
            model_resource_id="model_1",
            status="success",
        )
        attempt_row = attempt.to_db_model()
        self.assertIsInstance(attempt_row, AttemptLedger)
        self.assertEqual(attempt_row.request_id, "req_1")
        self.assertEqual(attempt_row.attempt_id, "att_1")
        self.assertEqual(attempt_row.provider_id, "conn_1")
        self.assertEqual(attempt_row.model, "model_1")
        self.assertEqual(attempt_row.status, "success")

    def test_usage_event_maps_to_db_model_with_provenance(self):
        from apps.gateway.db.models import UsageLedger
        from apps.gateway.usage.ledger import UsageEvent

        event = UsageEvent.from_parsed_usage(
            request_id="req_1",
            attempt_id="att_1",
            provider_connection_id="conn_1",
            credential_id="cred_1",
            model_resource_id="model_1",
            usage={
                "input_tokens": 10,
                "output_tokens": 20,
                "cached_input_tokens": 3,
                "cache_write_tokens": 2,
                "reasoning_tokens": 5,
                "native_metric": "tokens",
                "native_amount": 40.0,
                "actual_cost": 0.0123,
                "currency": "USD",
                "source": "provider_api",
                "confidence": "exact",
                "estimated": False,
            },
        )

        row = event.to_db_model(id="led_1")
        self.assertIsInstance(row, UsageLedger)
        self.assertEqual(row.id, "led_1")
        self.assertEqual(row.request_id, "req_1")
        self.assertEqual(row.attempt_id, "att_1")
        self.assertEqual(row.provider_id, "conn_1")
        self.assertEqual(row.credential_id, "cred_1")
        self.assertEqual(row.model, "model_1")
        self.assertEqual(row.prompt_tokens, 10)
        self.assertEqual(row.completion_tokens, 20)
        self.assertEqual(row.cached_input_tokens, 3)
        self.assertEqual(row.cache_write_tokens, 2)
        self.assertEqual(row.reasoning_tokens, 5)
        self.assertEqual(row.total_tokens, 30)
        self.assertEqual(row.native_metric, "tokens")
        self.assertEqual(row.native_amount, 40.0)
        self.assertEqual(row.estimated_cost, 0.0123)
        self.assertEqual(row.currency, "USD")
        self.assertEqual(row.source, "provider_api")
        self.assertEqual(row.confidence, "exact")
        self.assertFalse(row.estimated)


class UsageLedgerRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_records_request_and_attempt(self):
        from apps.gateway.db.models import AttemptLedger, RequestLedger
        from apps.gateway.usage.ledger import AttemptRecord, RequestRecord, UsageLedgerRepository

        session = FakeAsyncSession()
        repository = UsageLedgerRepository(session)
        request = RequestRecord(
            request_id="req_1",
            route_id="chat",
            logical_model="smart-chat",
            metadata={"tenant": "local-dev"},
        )
        attempt = AttemptRecord(
            request_id="req_1",
            attempt_id="att_1",
            provider_connection_id="conn_1",
            model_resource_id="model_1",
            status="success",
        )

        request_row = await repository.record_request(request)
        attempt_row = await repository.record_attempt(attempt, commit=True)

        self.assertIsInstance(request_row, RequestLedger)
        self.assertIsInstance(attempt_row, AttemptLedger)
        self.assertEqual(session.added, [request_row, attempt_row])
        self.assertTrue(session.flushed)
        self.assertTrue(session.committed)

    async def test_repository_records_usage_event_with_flush_only_by_default(self):
        from apps.gateway.db.models import UsageLedger
        from apps.gateway.usage.ledger import UsageEvent, UsageLedgerRepository

        session = FakeAsyncSession()
        repository = UsageLedgerRepository(session)
        event = UsageEvent.from_parsed_usage(
            request_id="req_1",
            attempt_id="att_1",
            provider_connection_id="conn_1",
            credential_id="cred_1",
            model_resource_id="model_1",
            usage={"input_tokens": 10, "output_tokens": 20, "source": "provider_api", "confidence": "exact"},
        )

        row = await repository.record_usage(event, id="led_1")

        self.assertIsInstance(row, UsageLedger)
        self.assertEqual(session.added, [row])
        self.assertTrue(session.flushed)
        self.assertFalse(session.committed)
        self.assertEqual(row.request_id, "req_1")
        self.assertEqual(row.attempt_id, "att_1")
        self.assertEqual(row.total_tokens, 30)

    async def test_repository_can_commit_when_requested(self):
        from apps.gateway.usage.ledger import UsageEvent, UsageLedgerRepository

        session = FakeAsyncSession()
        repository = UsageLedgerRepository(session)
        event = UsageEvent.from_parsed_usage(
            request_id="req_1",
            attempt_id="att_1",
            provider_connection_id="conn_1",
            credential_id=None,
            model_resource_id="model_1",
            usage={"input_tokens": 1, "output_tokens": 2},
        )

        await repository.record_usage(event, id="led_1", commit=True)

        self.assertTrue(session.flushed)
        self.assertTrue(session.committed)


if __name__ == "__main__":
    unittest.main()
