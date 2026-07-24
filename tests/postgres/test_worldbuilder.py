"""Hermetic Postgres Worldbuilder / promote coverage. DATABASE_URL only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.postgres.helpers import make_doc

pytestmark = pytest.mark.postgres

USER_ALICE = "user:11111111-1111-1111-1111-111111111111"
USER_BOB = "user:99999999-9999-9999-9999-999999999999"
PLATFORM_USER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _FakeRetrieval:
    def search(self, *args, **kwargs):
        return SimpleNamespace(passages=[], audit_id="audit:hermetic")


class _FakeSynth:
    model_name = "hermetic-synth"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str, *, json_object: bool = False):
        self.calls += 1
        # Invalid JSON on purpose — graph seeding must still fill structured fields.
        return "not-json prose about the subject", 12


def test_list_category_four_types_and_acl(hermetic_workspace):
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import DocumentParticipant, Entity, Person
    from org_memory.domain.models import Principal
    from org_memory.services.worldbuilder import WorldbuilderService

    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    public_id = f"test:wb-public-{hermetic_workspace}"
    private_id = f"test:wb-private-{hermetic_workspace}"
    person_id = f"person:{uuid.uuid4()}"
    team_pub = f"entity:{uuid.uuid4()}"
    team_priv = f"entity:{uuid.uuid4()}"
    project_id = f"entity:{uuid.uuid4()}"
    glossary_id = f"entity:{uuid.uuid4()}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
            )
        )
        session.add(
            make_doc(
                doc_id=private_id,
                workspace_id=hermetic_workspace,
                org_visible=False,
                allowed_principals=[USER_ALICE],
                event_time=t0,
            )
        )
        session.add(
            Person(
                canonical_id=person_id,
                workspace_id=hermetic_workspace,
                display_name="Ada Lovelace",
                resolution_status="resolved",
            )
        )
        # Flush person before FK children; ORM has no relationship() to order inserts.
        session.flush()
        session.add(
            DocumentParticipant(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                role="author",
                identity_kind="person",
                source_system="test",
                external_id="ada",
                display_name="Ada Lovelace",
                person_id=person_id,
                observed_person_id=person_id,
            )
        )
        session.add(
            Entity(
                entity_id=team_pub,
                workspace_id=hermetic_workspace,
                entity_type="team",
                name="Payments",
                normalized_name="payments",
                evidence_doc_ids=[public_id],
            )
        )
        session.add(
            Entity(
                entity_id=team_priv,
                workspace_id=hermetic_workspace,
                entity_type="team",
                name="Secret Squad",
                normalized_name="secret squad",
                evidence_doc_ids=[private_id],
            )
        )
        session.add(
            Entity(
                entity_id=project_id,
                workspace_id=hermetic_workspace,
                entity_type="project",
                name="Billing Rewrite",
                normalized_name="billing rewrite",
                evidence_doc_ids=[public_id],
            )
        )
        session.add(
            Entity(
                entity_id=glossary_id,
                workspace_id=hermetic_workspace,
                entity_type="glossary",
                name="CarePod",
                normalized_name="carepod",
                evidence_doc_ids=[public_id],
            )
        )

    alice = Principal(principal_id=USER_ALICE)
    bob = Principal(principal_id=USER_BOB)
    with session_scope() as session:
        wb = WorldbuilderService(session, _FakeRetrieval(), _FakeSynth())
        people = wb.list_category(alice, category="person", limit=50)
        assert any(i["canonical_id"] == person_id for i in people["items"])
        assert people["items"][0]["category"] == "person"

        teams_alice = wb.list_category(alice, category="team", limit=50)
        team_names = {i["display_name"] for i in teams_alice["items"]}
        assert "Payments" in team_names
        assert "Secret Squad" in team_names

        teams_bob = wb.list_category(bob, category="team", limit=50)
        bob_names = {i["display_name"] for i in teams_bob["items"]}
        assert "Payments" in bob_names
        assert "Secret Squad" not in bob_names

        projects = wb.list_category(bob, category="project", limit=50)
        assert any(i["canonical_id"] == project_id for i in projects["items"])
        glossary = wb.list_category(bob, category="glossary", limit=50)
        assert any(i["canonical_id"] == glossary_id for i in glossary["items"])


def test_resolve_about_person_vs_entity(hermetic_workspace):
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import DocumentParticipant, Entity, Person
    from org_memory.domain.models import Principal
    from org_memory.services.worldbuilder import WorldbuilderService

    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    public_id = f"test:about-{hermetic_workspace}"
    person_id = f"person:{uuid.uuid4()}"
    team_id = f"entity:{uuid.uuid4()}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
            )
        )
        session.add(
            Person(
                canonical_id=person_id,
                workspace_id=hermetic_workspace,
                display_name="Grace Hopper",
            )
        )
        session.flush()
        session.add(
            DocumentParticipant(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                role="author",
                identity_kind="person",
                source_system="test",
                external_id="grace",
                display_name="Grace Hopper",
                person_id=person_id,
                observed_person_id=person_id,
            )
        )
        session.add(
            Entity(
                entity_id=team_id,
                workspace_id=hermetic_workspace,
                entity_type="team",
                name="Compiler Team",
                normalized_name="compiler team",
                evidence_doc_ids=[public_id],
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        wb = WorldbuilderService(session, _FakeRetrieval(), _FakeSynth())
        person = wb.resolve_about_subject(principal, "Grace Hopper", category="person")
        assert person["kind"] == "person"
        assert person["about_person_ids"] == [person_id]

        team = wb.resolve_about_subject(principal, "Compiler Team", category="team")
        assert team["kind"] == "entity"
        assert team["about_doc_ids"] == [public_id]
        assert team["canonical_id"] == team_id


def test_read_source_claim_acl(hermetic_workspace):
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim
    from org_memory.domain.models import Principal
    from org_memory.services.worldbuilder import WorldbuilderService

    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    public_id = f"test:rs-public-{hermetic_workspace}"
    private_id = f"test:rs-private-{hermetic_workspace}"
    claim_pub = f"claim:{uuid.uuid4()}"
    claim_priv = f"claim:{uuid.uuid4()}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
                rendered_text="public claim evidence",
            )
        )
        session.add(
            make_doc(
                doc_id=private_id,
                workspace_id=hermetic_workspace,
                org_visible=False,
                allowed_principals=[USER_ALICE],
                event_time=t0,
                rendered_text="private claim evidence",
            )
        )
        session.add(
            Claim(
                claim_id=claim_pub,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=f"person:{uuid.uuid4()}",
                predicate="title",
                object_text="Engineer",
                confidence=1.0,
                status="active",
                evidence_doc_ids=[public_id],
                created_by="test",
            )
        )
        session.add(
            Claim(
                claim_id=claim_priv,
                workspace_id=hermetic_workspace,
                subject_type="person",
                subject_id=f"person:{uuid.uuid4()}",
                predicate="title",
                object_text="Secret Role",
                confidence=1.0,
                status="active",
                evidence_doc_ids=[private_id],
                created_by="test",
            )
        )

    bob = Principal(principal_id=USER_BOB)
    with session_scope() as session:
        wb = WorldbuilderService(session, _FakeRetrieval(), _FakeSynth())
        result = wb.read_source(
            bob,
            document_ids=[public_id, private_id],
            record_ids=[claim_pub, claim_priv],
        )
        outcomes = {o["id"]: o["outcome"] for o in result["outcomes"]}
        assert outcomes[public_id] == "ok"
        assert outcomes[private_id] == "forbidden"
        assert outcomes[claim_pub] == "ok"
        assert outcomes[claim_priv] == "forbidden"
        kinds = {s.get("source_record_id") or s.get("doc_id") for s in result["sources"]}
        assert public_id in kinds
        assert claim_pub in kinds
        assert private_id not in kinds
        assert claim_priv not in kinds


def test_profile_cache_hit_and_graph_seed(hermetic_workspace):
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim, Entity
    from org_memory.domain.models import Passage, Principal
    from org_memory.services.worldbuilder import WorldbuilderService

    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    public_id = f"test:cache-{hermetic_workspace}"
    entity_id = f"entity:{uuid.uuid4()}"
    claim_id = f"claim:{uuid.uuid4()}"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=public_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
            )
        )
        session.add(
            Entity(
                entity_id=entity_id,
                workspace_id=hermetic_workspace,
                entity_type="team",
                name="Cache Team",
                normalized_name="cache team",
                evidence_doc_ids=[public_id],
            )
        )
        session.add(
            Claim(
                claim_id=claim_id,
                workspace_id=hermetic_workspace,
                subject_type="team",
                subject_id=entity_id,
                predicate="title",
                object_text="Owns billing",
                confidence=0.95,
                status="active",
                evidence_doc_ids=[public_id],
                created_by="test",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    synth = _FakeSynth()
    evidence = [
        Passage(
            chunk_id="c1",
            doc_id=public_id,
            title="Cache Team doc",
            text="Cache Team owns billing",
            score=1.0,
            source_type="test_doc",
            source_system="test",
            event_time=t0,
            author_display_name="tester",
            deep_link="",
        )
    ]

    with session_scope() as session:
        from org_memory.db.repositories import GraphRepository

        graph = GraphRepository(session)
        claims = graph.claims_for_viewer("team", entity_id, principal, statuses=["active"])
        rels = graph.relationships_for_viewer("team", entity_id, principal)
        wb = WorldbuilderService(session, _FakeRetrieval(), synth)
        first = wb._synthesize_profile(
            principal=principal,
            category="team",
            subject_id=entity_id,
            display_name="Cache Team",
            resolution_status="provisional",
            platform_user_id=None,
            relationships=rels,
            claims=claims,
            evidence=evidence,
            audit_id="audit:1",
            query="Cache Team",
        )
        assert first["cache_hit"] is False
        assert first["profile_structure_source"] == "graph"
        assert first["subject_descriptions"]
        assert first["subject_descriptions"][0]["source_record_ids"] == [claim_id]
        assert synth.calls == 1

        second = wb._synthesize_profile(
            principal=principal,
            category="team",
            subject_id=entity_id,
            display_name="Cache Team",
            resolution_status="provisional",
            platform_user_id=None,
            relationships=rels,
            claims=claims,
            evidence=evidence,
            audit_id="audit:1",
            query="Cache Team",
        )
        assert second["cache_hit"] is True
        assert second["trace_id"] == first["trace_id"]
        assert synth.calls == 1
        assert second["subject_descriptions"][0]["text"] == "title: Owns billing"


def test_promote_autofills_platform_user_against_db(hermetic_workspace):
    from org_memory.db.engine import session_scope
    from org_memory.db.orm import Claim, DocumentParticipant, Person, PersonAlias
    from org_memory.db.repositories import PersonRepository
    from org_memory.domain.identity_namespaces import PLATFORM_USER_SOURCE_SYSTEM
    from org_memory.domain.models import Principal
    from org_memory.services.promotions import PromotionService

    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    doc_id = f"test:promote-{hermetic_workspace}"
    person_id = f"person:{uuid.uuid4()}"
    evidence_text = "Ada is Staff Engineer on Payments"

    with session_scope() as session:
        session.add(
            make_doc(
                doc_id=doc_id,
                workspace_id=hermetic_workspace,
                org_visible=True,
                allowed_principals=[],
                event_time=t0,
                rendered_text=evidence_text,
            )
        )
        session.add(
            Person(
                canonical_id=person_id,
                workspace_id=hermetic_workspace,
                display_name="Ada Lovelace",
                resolution_status="resolved",
            )
        )
        session.flush()
        session.add(
            DocumentParticipant(
                doc_id=doc_id,
                workspace_id=hermetic_workspace,
                role="author",
                identity_kind="person",
                source_system="test",
                external_id="ada",
                display_name="Ada Lovelace",
                person_id=person_id,
                observed_person_id=person_id,
            )
        )
        session.add(
            PersonAlias(
                person_id=person_id,
                observed_person_id=person_id,
                workspace_id=hermetic_workspace,
                source_system=PLATFORM_USER_SOURCE_SYSTEM,
                external_id=PLATFORM_USER,
                display_name="Ada Lovelace",
            )
        )

    principal = Principal(principal_id=USER_ALICE)
    with session_scope() as session:
        assert (
            PersonRepository(session).platform_user_id_for(person_id) == PLATFORM_USER
        )
        result = PromotionService(session).promote(
            principal=principal,
            om_canonical_id=person_id,
            subject_type="person",
            taxonomy_key="person",
            field_key="title",
            value="Staff Engineer",
            evidence_doc_ids=[doc_id],
            host_entity_id="",
            evidence_quote="Staff Engineer",
        )
        from org_memory.db.orm import TaxonomyProposal

        proposal = session.get(TaxonomyProposal, result["proposal_id"])
        assert proposal is not None
        assert proposal.host_entity_id == PLATFORM_USER
        claim = session.get(Claim, result["claim_id"])
        assert claim is not None
        assert claim.object_text == "Staff Engineer"
        assert claim.subject_id == person_id
