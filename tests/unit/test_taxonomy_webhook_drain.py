"""Taxonomy webhook push drains leftovers and requeues after singleton runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from org_memory.db.orm import utcnow
from org_memory.db.repositories.jobs import JobRepository
from org_memory.domain.jobs import JobType
from org_memory.workers.handlers.proposals import handle_push_taxonomy_proposal_webhook


def test_push_handler_returns_true_when_new_pending_appear(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-webhook")
    monkeypatch.setenv(
        "TAXONOMY_PROPOSAL_WEBHOOK_URL",
        "https://example.test/hooks/taxonomy",
    )
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()

    first = SimpleNamespace(proposal_id="p1", status="pending")
    second = SimpleNamespace(proposal_id="p2", status="pending")
    session = MagicMock()
    repo = MagicMock()
    repo.list_pending.side_effect = [[first], [first, second]]

    with (
        patch(
            "org_memory.workers.handlers.proposals.TaxonomyProposalRepository",
            return_value=repo,
        ),
        patch(
            "org_memory.services.proposal_webhook.push_proposals_if_configured",
        ) as push,
    ):
        needs_another = handle_push_taxonomy_proposal_webhook(session, {})

    assert needs_another is True
    push.assert_called_once()
    assert repo.list_pending.call_count == 2
    session.expire_all.assert_called_once()
    get_settings.cache_clear()


def test_push_handler_returns_false_when_batch_unchanged(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-webhook")
    monkeypatch.setenv(
        "TAXONOMY_PROPOSAL_WEBHOOK_URL",
        "https://example.test/hooks/taxonomy",
    )
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()

    first = SimpleNamespace(proposal_id="p1", status="pending")
    session = MagicMock()
    repo = MagicMock()
    repo.list_pending.side_effect = [[first], [first]]

    with (
        patch(
            "org_memory.workers.handlers.proposals.TaxonomyProposalRepository",
            return_value=repo,
        ),
        patch(
            "org_memory.services.proposal_webhook.push_proposals_if_configured",
        ),
    ):
        needs_another = handle_push_taxonomy_proposal_webhook(session, {})

    assert needs_another is False
    get_settings.cache_clear()


def test_push_handler_noop_without_webhook_url(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-webhook")
    monkeypatch.delenv("TAXONOMY_PROPOSAL_WEBHOOK_URL", raising=False)
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()
    assert handle_push_taxonomy_proposal_webhook(MagicMock(), {}) is False
    get_settings.cache_clear()


def test_refresh_running_push_sets_needs_another_pass(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-jobs")
    from org_memory.core.settings import get_settings

    get_settings.cache_clear()

    session = MagicMock()
    jobs = JobRepository(session)
    job = SimpleNamespace(
        job_id="job-1",
        job_type=JobType.push_taxonomy_proposal_webhook.value,
        status="running",
        locked_until=utcnow() + timedelta(seconds=60),
        payload={"proposal_ids": ["old"]},
        run_after=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    jobs._refresh_open_job(job, {"proposal_ids": ["new"]})
    assert job.payload["needs_another_pass"] is True
    assert job.payload["proposal_ids"] == ["new"]
    assert job.status == "running"

    get_settings.cache_clear()


def test_worker_requeues_push_after_mark_done(monkeypatch) -> None:
    from tests.conftest import apply_minimal_settings_env

    apply_minimal_settings_env(monkeypatch, workspace_id="ws-worker-push")
    from org_memory.core.settings import get_settings
    from org_memory.workers.run import Worker

    get_settings.cache_clear()
    settings = get_settings()

    job = SimpleNamespace(
        job_id="job-push",
        job_type=JobType.push_taxonomy_proposal_webhook.value,
        payload={"needs_another_pass": True},
        attempts=1,
        status="running",
    )
    jobs_repo = MagicMock()
    jobs_repo.claim_next.return_value = job

    session = MagicMock()
    worker = Worker.__new__(Worker)
    worker._settings = settings
    worker._handlers = {
        JobType.push_taxonomy_proposal_webhook: lambda s, j, jr: True,
    }

    with patch("org_memory.workers.run.session_scope") as scope:
        scope.return_value.__enter__.return_value = session
        scope.return_value.__exit__.return_value = False
        with patch(
            "org_memory.workers.run.JobRepository",
            return_value=jobs_repo,
        ):
            assert worker.process_one() is True

    jobs_repo.mark_done.assert_called_once_with(job)
    jobs_repo.enqueue.assert_called_once_with(
        JobType.push_taxonomy_proposal_webhook, {}
    )
    get_settings.cache_clear()
