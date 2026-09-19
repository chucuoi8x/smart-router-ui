"""PR-09 runtime quota index acceptance tests."""
import pytest

from apps.gateway.quota.reservations import QuotaReservationRequest, QuotaResource
from apps.gateway.quota.runtime_index import RuntimeQuotaIndex


def resources():
    return [
        QuotaResource("rpm:a", "credential:a", "requests", 100, 60, used=10),
        QuotaResource("tpm:a", "credential:a", "tokens", 1000, 60, used=100),
        QuotaResource("rpm:b", "credential:b", "requests", 100, 60, used=99),
    ]


@pytest.mark.asyncio
async def test_replace_all_works_inside_running_event_loop():
    index = RuntimeQuotaIndex()
    index.replace_all(resources())
    assert index.loaded
    assert index.snapshot("rpm:a").used == 10


def test_request_reads_scale_with_selected_resource_ids_only():
    index = RuntimeQuotaIndex()
    index.replace_all(resources())

    selected = index.resources_for(["rpm:a", "tpm:a", "missing"])

    assert set(selected) == {"rpm:a", "tpm:a"}
    admission = index.check_many([QuotaReservationRequest("rpm:a", 1)])
    assert admission.accepted is True


def test_atomic_copy_on_write_update_preserves_other_resources():
    index = RuntimeQuotaIndex()
    index.replace_all(resources())

    index.apply_update(QuotaResource("rpm:a", "credential:a", "requests", 100, 60, used=90))

    assert index.snapshot("rpm:a").used == 90
    assert index.snapshot("tpm:a").used == 100
    assert index.snapshot("rpm:b").used == 99


def test_generator_resources_are_not_consumed_twice():
    index = RuntimeQuotaIndex()
    index.replace_all(item for item in resources())
    assert index.snapshot("rpm:a").used == 10


def test_published_metadata_is_isolated_from_writers_and_readers():
    index = RuntimeQuotaIndex()
    metadata = {"nested": {"value": 1}}
    resource = QuotaResource("rpm", "credential", "requests", 100, 60,
                             window_metadata=metadata)
    index.replace_all([resource])
    metadata["nested"]["value"] = 2
    assert index.snapshot("rpm").window_metadata["nested"]["value"] == 1
    index.snapshot("rpm").window_metadata["nested"]["value"] = 3
    assert index.snapshot("rpm").window_metadata["nested"]["value"] == 1
    index.resources_for(["rpm"])["rpm"].window_metadata["nested"]["value"] = 4
    assert index.get_resource("rpm").window_metadata["nested"]["value"] == 1


def test_request_path_does_not_require_store_or_scan():
    index = RuntimeQuotaIndex()
    index.replace_all(resources())

    # All request-time methods are synchronous local reads. No Redis/store object
    # is retained by the index after publication.
    assert not hasattr(index, "store")
    assert index.get_effective_remaining("rpm:a") == 90
