"""Single initial schema (squashed).

Creates the full Postgres shape this app expects today. There is no migration
history before this revision — fresh databases only need `alembic upgrade head`.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE documents (
            doc_id               text PRIMARY KEY,
            workspace_id         text NOT NULL,
            source_system        text NOT NULL,
            external_id          text NOT NULL,
            source_type          text NOT NULL,
            title                text NOT NULL DEFAULT '',
            rendered_text        text NOT NULL DEFAULT '',
            author_external_id   text NOT NULL DEFAULT '',
            author_display_name  text NOT NULL DEFAULT '',
            author_email         text NOT NULL DEFAULT '',
            event_time           timestamptz NOT NULL,
            ingested_at          timestamptz NOT NULL DEFAULT now(),
            updated_at           timestamptz NOT NULL DEFAULT now(),
            org_visible          boolean NOT NULL DEFAULT false,
            allowed_principals   text[] NOT NULL DEFAULT '{}',
            acl_version          integer NOT NULL DEFAULT 1,
            acl_event_time       timestamptz NOT NULL DEFAULT now(),
            parent_external_id   text NOT NULL DEFAULT '',
            deep_link            text NOT NULL DEFAULT '',
            doc_metadata         jsonb NOT NULL DEFAULT '{}',
            raw_blob_key         text NOT NULL DEFAULT '',
            deleted              boolean NOT NULL DEFAULT false
        )
    """)
    op.execute("CREATE INDEX ix_documents_workspace ON documents (workspace_id)")
    op.execute("CREATE INDEX ix_documents_source ON documents (source_system, external_id)")

    op.execute("""
        CREATE TABLE chunks (
            chunk_id             text PRIMARY KEY,
            doc_id               text NOT NULL REFERENCES documents (doc_id),
            workspace_id         text NOT NULL,
            chunk_index          integer NOT NULL,
            text                 text NOT NULL,
            embedding            vector(1536),
            embedding_model      text,
            source_type          text NOT NULL,
            title                text NOT NULL DEFAULT '',
            author_display_name  text NOT NULL DEFAULT '',
            event_time           timestamptz NOT NULL,
            updated_at           timestamptz NOT NULL DEFAULT now(),
            deep_link            text NOT NULL DEFAULT '',
            org_visible          boolean NOT NULL DEFAULT false,
            allowed_principals   text[] NOT NULL DEFAULT '{}',
            deleted              boolean NOT NULL DEFAULT false,
            text_search tsvector GENERATED ALWAYS AS (
                setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(text, '')), 'B')
            ) STORED
        )
    """)
    op.execute("CREATE INDEX ix_chunks_doc ON chunks (doc_id)")
    op.execute("CREATE INDEX ix_chunks_workspace ON chunks (workspace_id)")
    op.execute("CREATE INDEX ix_chunks_event_time ON chunks (event_time)")
    op.execute("""
        CREATE INDEX ix_chunks_embedding_hnsw ON chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
    op.execute("CREATE INDEX ix_chunks_text_search ON chunks USING gin (text_search)")
    op.execute("CREATE INDEX ix_chunks_principals ON chunks USING gin (allowed_principals)")
    op.execute("""
        CREATE INDEX ix_chunks_unembedded ON chunks (chunk_id)
        WHERE embedding IS NULL AND deleted = false
    """)

    op.execute("""
        CREATE TABLE persons (
            canonical_id             text PRIMARY KEY,
            workspace_id             text NOT NULL,
            display_name             text NOT NULL,
            name_aliases             text[] NOT NULL DEFAULT '{}',
            primary_email            text NOT NULL DEFAULT '',
            resolution_status        text NOT NULL DEFAULT 'provisional',
            identity_metadata        jsonb NOT NULL DEFAULT '{}',
            merged_into_id           text REFERENCES persons (canonical_id),
            identity_embedding       vector(1536),
            identity_embedding_model text,
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_persons_name ON persons (display_name)")
    op.execute("""
        CREATE INDEX ix_persons_identity_embedding_hnsw ON persons
        USING hnsw (identity_embedding vector_cosine_ops)
    """)

    op.execute("""
        CREATE TABLE person_aliases (
            alias_id            text PRIMARY KEY,
            person_id           text NOT NULL REFERENCES persons (canonical_id),
            observed_person_id  text NOT NULL REFERENCES persons (canonical_id),
            workspace_id        text NOT NULL,
            source_system       text NOT NULL,
            external_id         text NOT NULL DEFAULT '',
            display_name        text NOT NULL DEFAULT '',
            email               text NOT NULL DEFAULT '',
            email_verified      boolean NOT NULL DEFAULT false,
            confidence          double precision NOT NULL DEFAULT 1.0,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_aliases_email ON person_aliases (email)")
    op.execute("CREATE INDEX ix_aliases_source ON person_aliases (source_system, external_id)")
    op.execute("CREATE INDEX ix_aliases_observed_person ON person_aliases (workspace_id, observed_person_id)")
    op.execute("""
        CREATE UNIQUE INDEX uq_alias_source_identity
        ON person_aliases (workspace_id, source_system, external_id)
        WHERE external_id <> ''
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_alias_email_only
        ON person_aliases (workspace_id, observed_person_id, source_system, email)
        WHERE external_id = '' AND email <> ''
    """)

    op.execute("""
        CREATE TABLE document_participants (
            participant_id      text PRIMARY KEY,
            doc_id              text NOT NULL REFERENCES documents (doc_id) ON DELETE CASCADE,
            workspace_id        text NOT NULL,
            role                text NOT NULL,
            identity_kind       text NOT NULL,
            source_system       text NOT NULL,
            external_id         text NOT NULL DEFAULT '',
            display_name        text NOT NULL DEFAULT '',
            emails              jsonb NOT NULL DEFAULT '[]',
            identifiers         jsonb NOT NULL DEFAULT '[]',
            person_id           text REFERENCES persons (canonical_id),
            observed_person_id  text REFERENCES persons (canonical_id),
            created_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_document_participants_doc
        ON document_participants (workspace_id, doc_id)
    """)
    op.execute("""
        CREATE INDEX ix_document_participants_person
        ON document_participants (workspace_id, person_id)
        WHERE person_id IS NOT NULL
    """)

    op.execute("""
        CREATE TABLE extraction_windows (
            doc_id         text NOT NULL REFERENCES documents (doc_id) ON DELETE CASCADE,
            content_hash   text NOT NULL,
            window_index   integer NOT NULL,
            window_hash    text NOT NULL,
            parsed_output  jsonb NOT NULL,
            tokens         integer NOT NULL DEFAULT 0,
            created_at     timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (doc_id, content_hash, window_index)
        )
    """)

    op.execute("""
        CREATE TABLE entities (
            entity_id          text PRIMARY KEY,
            workspace_id       text NOT NULL,
            entity_type        text NOT NULL,
            name               text NOT NULL,
            normalized_name    text NOT NULL,
            description        text NOT NULL DEFAULT '',
            attributes         jsonb NOT NULL DEFAULT '{}',
            resolution_status  text NOT NULL DEFAULT 'provisional',
            evidence_doc_ids   text[] NOT NULL DEFAULT '{}',
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_entities_name
        ON entities (workspace_id, entity_type, normalized_name)
    """)

    op.execute("""
        CREATE TABLE relationships (
            relationship_id                 text PRIMARY KEY,
            workspace_id                    text NOT NULL,
            from_type                       text NOT NULL,
            from_id                         text NOT NULL,
            to_type                         text NOT NULL,
            to_id                           text NOT NULL,
            from_label                      text NOT NULL DEFAULT '',
            to_label                        text NOT NULL DEFAULT '',
            relationship_type               text NOT NULL,
            valid_from                      timestamptz,
            valid_to                        timestamptz,
            confidence                      double precision NOT NULL DEFAULT 1.0,
            status                          text NOT NULL DEFAULT 'proposed',
            evidence_doc_ids                text[] NOT NULL DEFAULT '{}',
            evidence_quotes                 jsonb NOT NULL DEFAULT '[]',
            origin_from_id                  text NOT NULL DEFAULT '',
            origin_to_id                    text NOT NULL DEFAULT '',
            superseded_by_relationship_id   text NOT NULL DEFAULT '',
            created_by                      text NOT NULL,
            decided_by                      text NOT NULL DEFAULT '',
            created_at                      timestamptz NOT NULL DEFAULT now(),
            updated_at                      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_relationships_from ON relationships (workspace_id, from_type, from_id)")
    op.execute("CREATE INDEX ix_relationships_to ON relationships (workspace_id, to_type, to_id)")
    op.execute("""
        CREATE INDEX ix_active_relationships_search ON relationships USING gin (
            to_tsvector(
                'english',
                coalesce(from_label, '') || ' ' ||
                coalesce(relationship_type, '') || ' ' ||
                coalesce(to_label, '')
            )
        ) WHERE status = 'active'
    """)

    op.execute("""
        CREATE TABLE claims (
            claim_id                text PRIMARY KEY,
            workspace_id            text NOT NULL,
            subject_type            text NOT NULL,
            subject_id              text NOT NULL,
            predicate               text NOT NULL,
            object_text             text NOT NULL,
            confidence              double precision NOT NULL DEFAULT 1.0,
            status                  text NOT NULL DEFAULT 'proposed',
            evidence_doc_ids        text[] NOT NULL DEFAULT '{}',
            evidence_quotes         jsonb NOT NULL DEFAULT '[]',
            origin_subject_id       text NOT NULL DEFAULT '',
            superseded_by_claim_id  text NOT NULL DEFAULT '',
            created_by              text NOT NULL,
            decided_by              text NOT NULL DEFAULT '',
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_claims_subject ON claims (workspace_id, subject_type, subject_id)")
    op.execute("""
        CREATE INDEX ix_active_claims_search ON claims USING gin (
            to_tsvector('english', coalesce(predicate, '') || ' ' || coalesce(object_text, ''))
        ) WHERE status = 'active'
    """)

    op.execute("""
        CREATE TABLE person_merge_decisions (
            decision_id           text PRIMARY KEY,
            workspace_id          text NOT NULL,
            subject_kind          text NOT NULL,
            a_id                  text NOT NULL,
            b_id                  text NOT NULL,
            verdict               text NOT NULL,
            confidence            double precision NOT NULL DEFAULT 0.0,
            reason                text NOT NULL DEFAULT '',
            signals               jsonb NOT NULL DEFAULT '[]',
            evidence_fingerprint  text NOT NULL DEFAULT '',
            status                text NOT NULL,
            decided_by            text NOT NULL DEFAULT '',
            decided_at            timestamptz,
            reversed_at           timestamptz,
            reversal_reason       text NOT NULL DEFAULT '',
            created_at            timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_person_merge_pair
        ON person_merge_decisions (workspace_id, a_id, b_id, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX ix_person_merge_fingerprint
        ON person_merge_decisions (workspace_id, evidence_fingerprint)
    """)

    op.execute("""
        CREATE TABLE document_versions (
            version_id    text PRIMARY KEY,
            doc_id        text NOT NULL REFERENCES documents (doc_id),
            workspace_id  text NOT NULL,
            change_kind   text NOT NULL,
            event_time    timestamptz NOT NULL,
            blob_key      text NOT NULL,
            payload_hash  text NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_doc_versions ON document_versions (doc_id, created_at)")

    op.execute("""
        CREATE TABLE legal_holds (
            hold_id       text PRIMARY KEY,
            workspace_id  text NOT NULL,
            scope_type    text NOT NULL,
            scope_value   text NOT NULL,
            reason        text NOT NULL,
            placed_by     text NOT NULL,
            placed_at     timestamptz NOT NULL DEFAULT now(),
            released_by   text NOT NULL DEFAULT '',
            released_at   timestamptz
        )
    """)
    op.execute("""
        CREATE INDEX ix_legal_holds_active
        ON legal_holds (workspace_id, scope_type, scope_value) WHERE released_at IS NULL
    """)

    op.execute("""
        CREATE TABLE connector_status (
            workspace_id     text NOT NULL,
            source_system    text NOT NULL,
            last_envelope_at timestamptz,
            last_event_time  timestamptz,
            envelopes_total  bigint NOT NULL DEFAULT 0,
            failures_total   bigint NOT NULL DEFAULT 0,
            last_error       text NOT NULL DEFAULT '',
            last_failure_at  timestamptz,
            recent_errors    jsonb NOT NULL DEFAULT '[]'::jsonb,
            updated_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (workspace_id, source_system)
        )
    """)

    op.execute("""
        CREATE TABLE synthesis_traces (
            trace_id         text PRIMARY KEY,
            workspace_id     text NOT NULL,
            principal_id     text NOT NULL,
            tool             text NOT NULL,
            subject          text NOT NULL DEFAULT '',
            model            text NOT NULL,
            input_doc_ids    text[] NOT NULL DEFAULT '{}',
            output_text      text NOT NULL,
            tokens           integer NOT NULL DEFAULT 0,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_traces_subject ON synthesis_traces (workspace_id, tool, subject)")

    op.execute("""
        CREATE TABLE procedural_memories (
            memory_id                  text PRIMARY KEY,
            workspace_id               text NOT NULL,
            agent_id                   text NOT NULL,
            run_id                     text NOT NULL,
            procedure_key              text NOT NULL,
            objective                  text NOT NULL,
            summary                    text NOT NULL,
            events                     jsonb NOT NULL,
            raw_synthesis              text NOT NULL,
            status                     text NOT NULL DEFAULT 'active',
            superseded_by_memory_id    text NOT NULL DEFAULT '',
            evidence_doc_ids           text[] NOT NULL DEFAULT '{}',
            org_visible                boolean NOT NULL DEFAULT false,
            allowed_principals         text[] NOT NULL DEFAULT '{}',
            synthesis_model            text NOT NULL,
            embedding                  vector(1536),
            embedding_model            text,
            created_by_principal       text NOT NULL,
            created_at                 timestamptz NOT NULL DEFAULT now(),
            updated_at                 timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_procedural_scope
        ON procedural_memories (workspace_id, agent_id, procedure_key, status)
    """)
    op.execute("""
        CREATE INDEX ix_procedural_principals
        ON procedural_memories USING gin (allowed_principals)
    """)
    op.execute("""
        CREATE INDEX ix_procedural_embedding_hnsw ON procedural_memories
        USING hnsw (embedding vector_cosine_ops)
    """)
    op.execute("""
        CREATE INDEX ix_procedural_search ON procedural_memories USING gin (
            to_tsvector('english', coalesce(objective, '') || ' ' || coalesce(summary, ''))
        ) WHERE status = 'active'
    """)

    op.execute("""
        CREATE TABLE taxonomy_proposals (
            proposal_id            text PRIMARY KEY,
            workspace_id           text NOT NULL,
            subject_type           text NOT NULL,
            subject_id             text NOT NULL,
            taxonomy_key           text NOT NULL,
            field_key              text NOT NULL,
            predicate              text NOT NULL,
            value_text             text NOT NULL,
            confidence             double precision NOT NULL DEFAULT 0.0,
            evidence_doc_ids       text[] NOT NULL DEFAULT '{}',
            source_claim_id        text NOT NULL DEFAULT '',
            precedence_class       text NOT NULL DEFAULT 'extraction_single',
            status                 text NOT NULL DEFAULT 'pending',
            superseded_by_id       text NOT NULL DEFAULT '',
            decided_by             text NOT NULL DEFAULT '',
            decided_at             timestamptz,
            last_push_error        text NOT NULL DEFAULT '',
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_taxonomy_proposals_pending
        ON taxonomy_proposals (workspace_id, status, created_at)
        WHERE status = 'pending'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_taxonomy_proposals_open_slot
        ON taxonomy_proposals (
            workspace_id, subject_type, subject_id, taxonomy_key, field_key
        )
        WHERE status = 'pending'
    """)

    op.execute("""
        CREATE TABLE collaboration_edges (
            edge_id            text PRIMARY KEY,
            workspace_id       text NOT NULL,
            person_a_id        text NOT NULL,
            person_b_id        text NOT NULL,
            edge_type          text NOT NULL DEFAULT 'co_participant',
            weight             double precision NOT NULL DEFAULT 0.0,
            evidence_doc_ids   text[] NOT NULL DEFAULT '{}',
            last_seen_at       timestamptz,
            directed           boolean NOT NULL DEFAULT false,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            CHECK (person_a_id < person_b_id OR directed = true)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_collab_undirected_pair
        ON collaboration_edges (workspace_id, person_a_id, person_b_id, edge_type)
        WHERE directed = false
    """)
    op.execute("""
        CREATE INDEX ix_collab_person_a
        ON collaboration_edges (workspace_id, person_a_id, weight DESC)
    """)
    op.execute("""
        CREATE INDEX ix_collab_person_b
        ON collaboration_edges (workspace_id, person_b_id, weight DESC)
    """)

    op.execute("""
        CREATE TABLE jobs (
            job_id        text PRIMARY KEY,
            workspace_id  text NOT NULL,
            job_type      text NOT NULL,
            payload       jsonb NOT NULL DEFAULT '{}',
            status        text NOT NULL DEFAULT 'pending',
            attempts      integer NOT NULL DEFAULT 0,
            max_attempts  integer NOT NULL DEFAULT 5,
            last_error    text NOT NULL DEFAULT '',
            raw_error     text NOT NULL DEFAULT '',
            locked_until  timestamptz,
            run_after     timestamptz NOT NULL DEFAULT now(),
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_jobs_claimable ON jobs (run_after, created_at)
        WHERE status IN ('pending', 'running')
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_extract_graph_open
        ON jobs (workspace_id, (payload->>'doc_id'))
        WHERE job_type = 'extract_graph'
          AND status IN ('pending', 'running')
          AND payload ? 'doc_id'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_adjudicate_persons_open
        ON jobs (
            workspace_id,
            LEAST(payload->>'person_a', payload->>'person_b'),
            GREATEST(payload->>'person_a', payload->>'person_b')
        )
        WHERE job_type = 'adjudicate_persons'
          AND status IN ('pending', 'running')
          AND payload ? 'person_a'
          AND payload ? 'person_b'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_resolve_claim_conflict_open
        ON jobs (
            workspace_id,
            (payload->>'subject_type'),
            (payload->>'subject_id'),
            (payload->>'predicate')
        )
        WHERE job_type = 'resolve_claim_conflict'
          AND status IN ('pending', 'running')
          AND payload ? 'subject_type'
          AND payload ? 'subject_id'
          AND payload ? 'predicate'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_embed_chunks_open
        ON jobs (workspace_id, (payload->>'doc_id'))
        WHERE job_type = 'embed_chunks'
          AND status IN ('pending', 'running')
          AND payload ? 'doc_id'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_refresh_identity_embedding_open
        ON jobs (workspace_id, (payload->>'person_id'))
        WHERE job_type = 'refresh_identity_embedding'
          AND status IN ('pending', 'running')
          AND payload ? 'person_id'
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_aggregate_collaboration_edges_open
        ON jobs (workspace_id)
        WHERE job_type = 'aggregate_collaboration_edges'
          AND status IN ('pending', 'running')
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_generate_taxonomy_proposals_open
        ON jobs (workspace_id)
        WHERE job_type = 'generate_taxonomy_proposals'
          AND status IN ('pending', 'running')
    """)
    op.execute("""
        CREATE UNIQUE INDEX ux_jobs_push_taxonomy_proposal_webhook_open
        ON jobs (workspace_id)
        WHERE job_type = 'push_taxonomy_proposal_webhook'
          AND status IN ('pending', 'running')
    """)

    op.execute("""
        CREATE TABLE spend_ledger (
            entry_id      text PRIMARY KEY,
            workspace_id  text NOT NULL,
            job_class     text NOT NULL,
            vendor        text NOT NULL,
            model         text NOT NULL,
            tokens        integer NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_spend_month ON spend_ledger (workspace_id, job_class, created_at)")

    op.execute("""
        CREATE TABLE retrieval_audits (
            audit_id           text PRIMARY KEY,
            workspace_id       text NOT NULL,
            principal_id       text NOT NULL,
            tool               text NOT NULL,
            query              text NOT NULL,
            params             jsonb NOT NULL DEFAULT '{}',
            result_chunk_ids   text[] NOT NULL DEFAULT '{}',
            result_fact_ids    text[] NOT NULL DEFAULT '{}',
            result_memory_ids  text[] NOT NULL DEFAULT '{}',
            created_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_audits_principal ON retrieval_audits (principal_id, created_at)")

    op.execute("""
        CREATE TABLE admin_audits (
            audit_id      text PRIMARY KEY,
            workspace_id  text NOT NULL,
            principal_id  text NOT NULL,
            action        text NOT NULL,
            params        jsonb NOT NULL DEFAULT '{}',
            created_at    timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX ix_admin_audits_workspace_created "
        "ON admin_audits (workspace_id, created_at DESC)"
    )


def downgrade() -> None:
    for table in (
        "admin_audits",
        "retrieval_audits",
        "spend_ledger",
        "jobs",
        "collaboration_edges",
        "taxonomy_proposals",
        "procedural_memories",
        "synthesis_traces",
        "connector_status",
        "legal_holds",
        "document_versions",
        "person_merge_decisions",
        "claims",
        "relationships",
        "entities",
        "extraction_windows",
        "document_participants",
        "person_aliases",
        "persons",
        "chunks",
        "documents",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
