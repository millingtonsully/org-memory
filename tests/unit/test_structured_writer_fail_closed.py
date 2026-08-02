"""Fail-closed structured field writes when author person is unresolved."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from org_memory.domain.models import StructuredField
from org_memory.services.structured_writers import RegistryBackedStructuredFieldWriter


def test_unbound_fields_without_person_are_noop(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-structured-fail")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    session = MagicMock()
    session.get.return_value = SimpleNamespace(
        doc_id="doc:1",
        workspace_id="ws-structured-fail",
        event_time=datetime(2026, 6, 1, tzinfo=UTC),
        source_system="hr",
        author_external_id=None,
        author_email=None,
        doc_metadata={},
    )
    writer = RegistryBackedStructuredFieldWriter()
    with patch(
        "org_memory.services.structured_writers._author_person_subject",
        return_value=None,
    ) as resolve:
        written = writer.apply(
            session,
            doc_id="doc:1",
            fields=[StructuredField(key="custom.unbound", value="x")],
        )
    assert written == []
    resolve.assert_not_called()


def test_bound_fields_without_person_fail_closed(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-structured-fail")
    from org_memory.core.settings import get_settings
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    session = MagicMock()
    session.get.return_value = SimpleNamespace(
        doc_id="doc:1",
        workspace_id="ws-structured-fail",
        event_time=datetime(2026, 6, 1, tzinfo=UTC),
        source_system="hr",
        author_external_id="emp-missing",
        author_email=None,
        doc_metadata={},
    )
    writer = RegistryBackedStructuredFieldWriter()
    with (
        patch(
            "org_memory.services.structured_writers._author_person_subject",
            return_value=None,
        ),
        pytest.raises(ValueError, match="resolvable author person") as exc,
    ):
        writer.apply(
            session,
            doc_id="doc:1",
            fields=[StructuredField(key="hr.title", value="Manager")],
        )
    assert "hr.title" in str(exc.value)


def test_bound_fields_with_person_write_claim(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-structured-fail")
    from org_memory.core.settings import get_settings
    from org_memory.domain.fact_lifecycle import FactStatus
    from org_memory.taxonomy_registry import clear_taxonomy_registry_cache

    get_settings.cache_clear()
    clear_taxonomy_registry_cache()

    session = MagicMock()
    session.get.return_value = SimpleNamespace(
        doc_id="doc:1",
        workspace_id="ws-structured-fail",
        event_time=datetime(2026, 6, 1, tzinfo=UTC),
        source_system="hr",
        author_external_id="emp-1",
        author_email=None,
        doc_metadata={},
    )
    claim = SimpleNamespace(
        claim_id="claim-1",
        subject_type="person",
        subject_id="person-1",
        predicate="title",
        status=FactStatus.active.value,
    )
    graph = MagicMock()
    graph.add_claim.return_value = claim
    jobs = MagicMock()
    writer = RegistryBackedStructuredFieldWriter()
    with (
        patch(
            "org_memory.services.structured_writers._author_person_subject",
            return_value=("person", "person-1"),
        ),
        patch(
            "org_memory.services.structured_writers.GraphRepository",
            return_value=graph,
        ),
        patch(
            "org_memory.services.structured_writers.JobRepository",
            return_value=jobs,
        ),
        patch(
            "org_memory.services.structured_writers.eager_close_claim_slot_and_enqueue_conflict",
        ),
    ):
        written = writer.apply(
            session,
            doc_id="doc:1",
            fields=[StructuredField(key="hr.title", value="Manager")],
        )
    assert written == ["claim-1"]
    graph.add_claim.assert_called_once()


def test_ingress_maps_structured_value_error_to_422() -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from org_memory.api.routes_ingress import ingest_envelope

    envelope = SimpleNamespace(source_system="hr")
    ingest = MagicMock()
    ingest.ingest_envelope.side_effect = ValueError(
        "Cannot apply registry-bound structured fields without a resolvable "
        "author person (doc_id=hr:emp-1; fields=hr.title)."
    )
    request = MagicMock()
    request.body = AsyncMock(return_value=b"{}")

    with (
        patch("org_memory.api.routes_ingress.session_scope"),
        patch("org_memory.api.routes_ingress.ConnectorStatusRepository"),
        patch("org_memory.core.metrics.INGEST_FAIL") as fail_metric,
        patch("org_memory.core.metrics.INGEST_OK"),
    ):
        fail_metric.inc = MagicMock()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(ingest_envelope(request, envelope, ingest))  # type: ignore[arg-type]

    assert exc.value.status_code == 422
    assert "resolvable author person" in str(exc.value.detail)
