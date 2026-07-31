"""Shared data models (no vendor imports).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from org_memory.domain.principals import require_principal


class ChangeKind(str, Enum):
    """What happened in the source system. Deletes become tombstones."""

    create = "create"
    update = "update"
    delete = "delete"
    permission_change = "permission_change"


class IdentityKind(str, Enum):
    """Source actor classification supplied by the connector."""

    person = "person"
    service = "service"
    shared = "shared"
    unknown = "unknown"


class IdentityEmail(BaseModel):
    """One source-observed address; verification must come from the source."""

    value: str
    verified: bool = False


class IdentityKey(BaseModel):
    """Namespaced cross-source identifier (IdP, HR, etc.).

    Reserved: namespace `platform_user` stores the host User UUID (same UUID as
    `user:<uuid>` principals) as PersonAlias `identity:platform_user`.
    """

    namespace: str
    value: str
    verified: bool = False


class SourceIdentity(BaseModel):
    """A participant as represented by one source system.

    Roles and identifiers are free strings. Connectors classify actors rather
    than forcing shared accounts and service identities into Person records.
    """

    role: str
    identity_kind: IdentityKind = IdentityKind.unknown
    external_id: str = ""
    display_name: str = ""
    emails: list[IdentityEmail] = Field(default_factory=list)
    identifiers: list[IdentityKey] = Field(default_factory=list)


class StructuredField(BaseModel):
    """Connector-native structured fact carried beside rendered text.
    """

    key: str = Field(description="Stable field key like jira.status or gcal.attendee")
    value: Any = Field(description="JSON-serializable value from the source")
    value_type: Literal["string", "number", "boolean", "datetime", "enum", "reference"] = "string"


class ChangeEnvelope(BaseModel):
    """One change from a connector (or a test file). POST this to /ingress/envelope.

    doc_id is built as "{source_system}:{external_id}". Replaying the same
    envelope upserts instead of creating a duplicate.
    """

    source_system: str = Field(description="Opaque connector/system id. Any free string is accepted.")
    external_id: str = Field(description="the source tool's own id for this object")
    change_kind: ChangeKind
    source_type: str = Field(description="Opaque object-kind label for filtering. Any free string works.")

    title: str = Field(default="", description="human-readable title")
    text: str = Field(default="", description="rendered text used for indexing")

    # Identity as the source system knows it. Entity resolution maps these
    # to a canonical Person.
    author_external_id: str = Field(default="", description="author id as known in the source system")
    author_display_name: str = Field(default="")
    author_email: str = Field(default="")
    author_identity_kind: IdentityKind = IdentityKind.unknown
    author_email_verified: bool = False
    participants: list[SourceIdentity] = Field(
        default_factory=list,
        description=(
            "All source-observed participants and roles. This is connector-agnostic; "
            "role and identifier namespaces are free strings."
        ),
    )

    event_time: datetime = Field(description="when this happened in the world")

    # Who may read this. If org_visible is false and allowed_principals is
    # empty, nobody can retrieve it. Principals must be user:<uuid> or group:<uuid>.
    org_visible: bool = Field(
        default=False,
        description="True only for content public to the whole workspace",
    )
    allowed_principals: list[str] = Field(
        default_factory=list,
        description="user/group ids allowed to read this content (platform UUID form)",
    )

    structured_fields: list[StructuredField] = Field(
        default_factory=list,
        description="Connector-native structured values for deterministic fact writers",
    )

    parent_external_id: str = Field(default="", description="thread/channel/page parent")
    deep_link: str = Field(default="", description="URL back to the source object")
    metadata: dict = Field(default_factory=dict)

    @field_validator("allowed_principals")
    @classmethod
    def _validate_principals(cls, values: list[str]) -> list[str]:
        return [require_principal(v, field="allowed_principals") for v in values]

    @model_validator(mode="after")
    def _require_principals_when_not_org_visible(self) -> ChangeEnvelope:
        """Fail closed: private content must name who may read it.

        Deletes are exempt (tombstone only). Empty + org_visible=false would
        store forever-invisible rows — usually a sync bug, not intent.
        """
        if self.change_kind == ChangeKind.delete:
            return self
        if not self.org_visible and not self.allowed_principals:
            raise ValueError(
                "org_visible=false requires nonempty allowed_principals "
                "(user:<uuid> / group:<uuid>); refusing invisible ingest"
            )
        return self

    def source_identities(self) -> list[SourceIdentity]:
        """Return legacy author fields plus structured participants."""
        identities = list(self.participants)
        if self.author_external_id or self.author_display_name or self.author_email:
            emails = (
                [IdentityEmail(value=self.author_email, verified=self.author_email_verified)]
                if self.author_email
                else []
            )
            identities.insert(
                0,
                SourceIdentity(
                    role="author",
                    identity_kind=self.author_identity_kind,
                    external_id=self.author_external_id,
                    display_name=self.author_display_name,
                    emails=emails,
                ),
            )
        return identities


class Principal(BaseModel):
    """Who's requesting. Bound from request headers on every retrieval call.

    A chunk is visible if the document is org_visible, or the caller's
    principal_id (or any of their groups) is in allowed_principals.
    """

    principal_id: str
    groups: list[str] = Field(default_factory=list)

    @field_validator("principal_id")
    @classmethod
    def _validate_principal_id(cls, value: str) -> str:
        return require_principal(value, field="principal_id")

    @field_validator("groups")
    @classmethod
    def _validate_groups(cls, values: list[str]) -> list[str]:
        return [require_principal(v, field="groups") for v in values]

    def all_principals(self) -> list[str]:
        return [self.principal_id, *self.groups]


class Passage(BaseModel):
    """One search hit with enough provenance for an agent to cite it."""

    chunk_id: str
    doc_id: str
    source_type: str
    title: str
    text: str
    author_display_name: str
    event_time: datetime
    deep_link: str
    source_system: str = ""
    updated_at: datetime | None = None
    score: float = Field(description="final score after fusion and decay (and rerank when used)")
    rank_debug: dict = Field(
        default_factory=dict,
        description="per-channel ranks for eval and audit",
    )


class FactPassage(BaseModel):
    """One structured fact with currently visible provenance."""

    fact_id: str
    fact_type: str
    text: str
    confidence: float
    evidence_doc_ids: list[str]
    evidence_quotes: list[dict] = Field(default_factory=list)
    status: str = "active"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    score: float
    rank_debug: dict = Field(default_factory=dict)


class SearchResponse(BaseModel):
    """Return shape of search_knowledge_base / worldbuilder_kb."""

    query: str
    passages: list[Passage]
    facts: list[FactPassage] = Field(default_factory=list)
    total_candidates: int
    reranked: bool
    audit_id: str = Field(description="retrieval audit row id")
