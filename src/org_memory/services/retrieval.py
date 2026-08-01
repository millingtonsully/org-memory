"""Hybrid search for search_knowledge_base and worldbuilder_kb.

Embed the query, fetch viewer-scoped vector and keyword candidates, fuse with
RRF, apply recency decay, then cross-encoder rerank when the shortlist is larger
than the final limit. Final order is rerank scores when reranked, otherwise
decayed RRF. Every search writes an audit row.

Diagnostics helpers live in ``retrieval_diagnostics``.
"""

from __future__ import annotations

from datetime import datetime

from org_memory.core.settings import get_settings
from org_memory.db.engine import session_scope
from org_memory.db.repositories import (
    AuditRepository,
    ChunkSearchRepository,
    GraphRepository,
    PersonRepository,
    SpendRepository,
)
from org_memory.domain.models import FactPassage, Passage, Principal, SearchResponse
from org_memory.ports.embedder import Embedder
from org_memory.ports.reranker import Reranker
from org_memory.services.ranking import recency_multiplier, rrf_fuse
from org_memory.services.retrieval_diagnostics import (
    build_search_diagnostics,
)
from org_memory.services.retrieval_diagnostics import (
    filter_diagnostics as _filter_diagnostics,
)
from org_memory.taxonomy_registry import get_taxonomy_registry


class RetrievalService:
    def __init__(
        self,
        search_repo: ChunkSearchRepository,
        audit_repo: AuditRepository,
        embedder: Embedder,
        reranker: Reranker,
        graph_repo: GraphRepository,
        person_repo: PersonRepository | None = None,
    ):
        self._search = search_repo
        self._audit = audit_repo
        self._embedder = embedder
        self._reranker = reranker
        self._graph = graph_repo
        self._persons = person_repo

    def _expand_author(self, author: str | None) -> list[str] | None:
        """Expand author filter through entity aliases; escape ILIKE wildcards."""
        if not author:
            return None

        def _escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        patterns = {f"%{_escape(author)}%"}
        if self._persons is not None:
            for person in self._persons.search_by_name(author, limit=3):
                patterns.add(f"%{_escape(person.display_name)}%")
                for alias in self._persons.aliases_for(person.canonical_id):
                    if alias.display_name:
                        patterns.add(f"%{_escape(alias.display_name)}%")
        return sorted(patterns)

    def _expand_canonical_author(self, person_id: str) -> list[str] | None:
        """Return [person_id] when the canonical person exists and is not merged away."""
        if self._persons is None:
            return None
        person = self._persons.get(person_id)
        if person is None or person.merged_into_id:
            return None
        return [person.canonical_id]

    def search(
        self,
        *,
        principal: Principal,
        query: str,
        limit: int = 10,
        source_type: str | None = None,
        source_system: str | None = None,
        author: str | None = None,
        author_canonical_entity_id: str | None = None,
        about_person_ids: list[str] | None = None,
        about_doc_ids: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        updated_from: datetime | None = None,
        updated_to: datetime | None = None,
        doc_id: str | None = None,
        half_life_days: float = 90.0,
        min_decay: float = 0.3,
        tool_name: str = "search_knowledge_base",
        mode: str = "hybrid",
        as_of: datetime | None = None,
        believed_as_of: datetime | None = None,
        as_of_grain: str | None = None,
    ) -> SearchResponse:
        settings = get_settings()
        candidate_pool = settings.rerank_candidates
        use_vector = mode in ("hybrid", "semantic")
        use_keyword = mode in ("hybrid", "keyword")
        if mode not in ("hybrid", "semantic", "keyword"):
            raise ValueError(f"unsupported retrieval mode: {mode!r}")

        with session_scope() as spend_session:
            SpendRepository(spend_session).assert_under_hard_limit()

        author_patterns = self._expand_author(author)
        author_person_ids: list[str] | None = None
        if author_canonical_entity_id:
            author_person_ids = self._expand_canonical_author(author_canonical_entity_id)
            if author_person_ids is None:
                audit_id = self._audit.record_retrieval(
                    principal=principal,
                    tool=tool_name,
                    query=query,
                    params={
                        "author_canonical_entity_id": author_canonical_entity_id,
                        "fail_closed": "unknown_canonical",
                    },
                    chunk_ids=[],
                )
                return SearchResponse(
                    query=query,
                    passages=[],
                    facts=[],
                    audit_id=audit_id,
                    total_candidates=0,
                    reranked=False,
                    diagnostics=build_search_diagnostics(
                        mode=mode,
                        limit=limit,
                        candidate_pool=candidate_pool,
                        rrf_k=settings.rrf_k,
                        vector_count=0,
                        keyword_count=0,
                        fact_count=0,
                        fused_count=0,
                        shortlist_count=0,
                        returned_passages=0,
                        returned_facts=0,
                        parent_dedupe_removed=0,
                        reranked=False,
                        rerank_skipped_reason="unknown_canonical_author",
                        filters=_filter_diagnostics(
                            source_type=source_type,
                            source_system=source_system,
                            author=author,
                            author_canonical_entity_id=author_canonical_entity_id,
                            about_person_ids=about_person_ids,
                            about_doc_ids=about_doc_ids,
                            date_from=date_from,
                            date_to=date_to,
                            updated_from=updated_from,
                            updated_to=updated_to,
                            doc_id=doc_id,
                        ),
                    ),
                )
            author_patterns = None
        vector_hits: list = []
        if use_vector:
            query_vectors, embed_tokens = self._embedder.embed_texts([query])
            with session_scope() as spend_session:
                SpendRepository(spend_session).record(
                    "embed", "embedding", self._embedder.model_name, embed_tokens
                )
            vector_hits = self._search.vector_candidates(
                query_embedding=query_vectors[0],
                embedding_model=self._embedder.model_name,
                principal=principal,
                limit=candidate_pool,
                source_type=source_type,
                source_system=source_system,
                author_patterns=author_patterns,
                date_from=date_from,
                date_to=date_to,
                updated_from=updated_from,
                updated_to=updated_to,
                doc_id=doc_id,
                author_person_ids=author_person_ids,
                about_person_ids=about_person_ids,
                about_doc_ids=about_doc_ids,
            )
        keyword_hits: list = []
        if use_keyword:
            keyword_hits = self._search.keyword_candidates(
                query_text=query,
                principal=principal,
                limit=candidate_pool,
                source_type=source_type,
                source_system=source_system,
                author_patterns=author_patterns,
                date_from=date_from,
                date_to=date_to,
                updated_from=updated_from,
                updated_to=updated_to,
                doc_id=doc_id,
                author_person_ids=author_person_ids,
                about_person_ids=about_person_ids,
                about_doc_ids=about_doc_ids,
            )
        fact_hits: list = []
        if use_keyword:
            fact_hits = self._graph.fact_candidates(
                query,
                principal,
                candidate_pool,
                source_type=source_type,
                source_system=source_system,
                author_patterns=author_patterns,
                date_from=date_from,
                date_to=date_to,
                updated_from=updated_from,
                updated_to=updated_to,
                doc_id=doc_id,
                author_person_ids=author_person_ids,
                about_person_ids=about_person_ids,
                about_doc_ids=about_doc_ids,
                as_of=as_of,
                believed_as_of=believed_as_of,
                as_of_grain=as_of_grain,
            )

        by_id: dict[str, dict] = {}
        vector_ranks: dict[str, int] = {}
        keyword_ranks: dict[str, int] = {}
        for hit in vector_hits:
            by_id.setdefault(hit["chunk_id"], hit)
            vector_ranks[hit["chunk_id"]] = hit["rank"]
        for hit in keyword_hits:
            by_id.setdefault(hit["chunk_id"], hit)
            keyword_ranks[hit["chunk_id"]] = hit["rank"]
        facts_by_id = {hit["fact_id"]: hit for hit in fact_hits}
        fact_ranks = {hit["fact_id"]: hit["rank"] for hit in fact_hits}

        if not by_id and not facts_by_id:
            audit_id = self._audit.record_retrieval(principal, tool_name, query, {"limit": limit}, [])
            return SearchResponse(
                query=query,
                passages=[],
                facts=[],
                total_candidates=0,
                reranked=False,
                audit_id=audit_id,
                diagnostics=build_search_diagnostics(
                    mode=mode,
                    limit=limit,
                    candidate_pool=candidate_pool,
                    rrf_k=settings.rrf_k,
                    vector_count=len(vector_hits),
                    keyword_count=len(keyword_hits),
                    fact_count=len(fact_hits),
                    fused_count=0,
                    shortlist_count=0,
                    returned_passages=0,
                    returned_facts=0,
                    parent_dedupe_removed=0,
                    reranked=False,
                    rerank_skipped_reason="no_visible_candidates",
                    filters=_filter_diagnostics(
                        source_type=source_type,
                        source_system=source_system,
                        author=author,
                        author_canonical_entity_id=author_canonical_entity_id,
                        about_person_ids=about_person_ids,
                        about_doc_ids=about_doc_ids,
                        date_from=date_from,
                        date_to=date_to,
                        updated_from=updated_from,
                        updated_to=updated_to,
                        doc_id=doc_id,
                    ),
                ),
            )

        chunk_fused_lists = [
            {f"chunk:{cid}": rank for cid, rank in vector_ranks.items()},
            {f"chunk:{cid}": rank for cid, rank in keyword_ranks.items()},
            {f"fact:{fid}": rank for fid, rank in fact_ranks.items()},
        ]
        fused = rrf_fuse(chunk_fused_lists, k=settings.rrf_k)

        decayed: dict[str, float] = {}
        registry = get_taxonomy_registry()
        for item_id, score in fused.items():
            if item_id.startswith("chunk:"):
                cid = item_id.removeprefix("chunk:")
                decayed[item_id] = score * recency_multiplier(
                    by_id[cid]["event_time"],
                    half_life_days=half_life_days,
                    min_decay=min_decay,
                )
            else:
                fid = item_id.removeprefix("fact:")
                fact = facts_by_id[fid]
                half = settings.fact_freshness_half_life_days
                pred = fact.get("predicate")
                if pred and registry.is_known_predicate(pred):
                    override = registry.predicates[pred].freshness_half_life_days
                    if override is not None:
                        half = override
                as_of_time = fact.get("valid_from") or fact.get("recorded_at")
                if as_of_time is None:
                    decayed[item_id] = score * settings.fact_freshness_min_decay
                else:
                    decayed[item_id] = score * recency_multiplier(
                        as_of_time,
                        half_life_days=half,
                        min_decay=settings.fact_freshness_min_decay,
                    )

        # Score descending, then item id ascending. The id tie-break keeps result
        # order reproducible when two candidates score identically.
        shortlist_ids = sorted(decayed, key=lambda iid: (-decayed[iid], iid))[:candidate_pool]
        if len(shortlist_ids) <= limit:
            final_ids = shortlist_ids[:limit]
            score_by_id = {item_id: decayed[item_id] for item_id in final_ids}
            did_rerank = False
            rerank_skipped_reason = "shortlist_within_limit"
        else:
            documents = [
                (
                    by_id[item_id.removeprefix("chunk:")]["text"]
                    if item_id.startswith("chunk:")
                    else facts_by_id[item_id.removeprefix("fact:")]["text"]
                )
                for item_id in shortlist_ids
            ]
            rerank_scores, rerank_tokens = self._reranker.rerank(query, documents)
            with session_scope() as spend_session:
                SpendRepository(spend_session).record(
                    "rerank", "rerank", self._reranker.model_name, rerank_tokens
                )
            score_by_id = dict(zip(shortlist_ids, rerank_scores, strict=True))
            final_ids = [
                item_id
                for item_id, _ in sorted(
                    score_by_id.items(), key=lambda item: (-item[1], item[0])
                )[:limit]
            ]
            did_rerank = True
            rerank_skipped_reason = None

        final_chunk_ids = [
            item_id.removeprefix("chunk:") for item_id in final_ids if item_id.startswith("chunk:")
        ]
        final_fact_ids = [
            item_id.removeprefix("fact:") for item_id in final_ids if item_id.startswith("fact:")
        ]

        # One passage per parent section when multiple children from the same parent hit.
        seen_parents: set[str] = set()
        deduped_chunk_ids: list[str] = []
        for cid in final_chunk_ids:
            parent_key = by_id[cid].get("parent_chunk_id") or cid
            if parent_key in seen_parents:
                continue
            seen_parents.add(parent_key)
            deduped_chunk_ids.append(cid)
        parent_dedupe_removed = len(final_chunk_ids) - len(deduped_chunk_ids)
        final_chunk_ids = deduped_chunk_ids

        passages = [
            Passage(
                chunk_id=cid,
                doc_id=by_id[cid]["doc_id"],
                source_type=by_id[cid]["source_type"],
                source_system=by_id[cid].get("source_system") or "",
                title=by_id[cid]["title"],
                text=by_id[cid]["text"],
                author_display_name=by_id[cid]["author_display_name"],
                event_time=by_id[cid]["event_time"],
                updated_at=by_id[cid].get("updated_at"),
                deep_link=by_id[cid]["deep_link"],
                score=score_by_id[f"chunk:{cid}"],
                rank_debug={
                    "vector_rank": vector_ranks.get(cid),
                    "keyword_rank": keyword_ranks.get(cid),
                    "rrf_decayed": decayed[f"chunk:{cid}"],
                    "rerank": score_by_id[f"chunk:{cid}"] if did_rerank else None,
                },
            )
            for cid in final_chunk_ids
        ]
        facts = [
            FactPassage(
                fact_id=fact_id,
                fact_type=facts_by_id[fact_id]["fact_type"],
                text=facts_by_id[fact_id]["text"],
                confidence=facts_by_id[fact_id]["confidence"],
                evidence_doc_ids=facts_by_id[fact_id]["evidence_doc_ids"],
                evidence_quotes=facts_by_id[fact_id].get("evidence_quotes") or [],
                status=facts_by_id[fact_id].get("status") or "active",
                valid_from=facts_by_id[fact_id].get("valid_from"),
                valid_to=facts_by_id[fact_id].get("valid_to"),
                score=score_by_id[f"fact:{fact_id}"],
                rank_debug={
                    "keyword": facts_by_id[fact_id]["keyword_score"],
                    "rrf": decayed.get(f"fact:{fact_id}"),
                    "rerank": score_by_id[f"fact:{fact_id}"] if did_rerank else None,
                },
            )
            for fact_id in final_fact_ids
        ]

        audit_id = self._audit.record_retrieval(
            principal,
            tool_name,
            query,
            {
                "limit": limit,
                "source_type": source_type,
                "source_system": source_system,
                "author": author,
                "about_person_ids": about_person_ids,
                "half_life_days": half_life_days,
                "min_decay": min_decay,
                "mode": mode,
            },
            final_chunk_ids,
            final_fact_ids,
        )

        return SearchResponse(
            query=query,
            passages=passages,
            facts=facts,
            total_candidates=len(by_id) + len(facts_by_id),
            reranked=did_rerank,
            audit_id=audit_id,
            diagnostics=build_search_diagnostics(
                mode=mode,
                limit=limit,
                candidate_pool=candidate_pool,
                rrf_k=settings.rrf_k,
                vector_count=len(vector_hits),
                keyword_count=len(keyword_hits),
                fact_count=len(fact_hits),
                fused_count=len(fused),
                shortlist_count=len(shortlist_ids),
                returned_passages=len(passages),
                returned_facts=len(facts),
                parent_dedupe_removed=parent_dedupe_removed,
                reranked=did_rerank,
                rerank_skipped_reason=rerank_skipped_reason,
                filters=_filter_diagnostics(
                    source_type=source_type,
                    source_system=source_system,
                    author=author,
                    author_canonical_entity_id=author_canonical_entity_id,
                    about_person_ids=about_person_ids,
                    about_doc_ids=about_doc_ids,
                    date_from=date_from,
                    date_to=date_to,
                    updated_from=updated_from,
                    updated_to=updated_to,
                    doc_id=doc_id,
                ),
            ),
        )
