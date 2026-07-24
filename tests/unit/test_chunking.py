"""Chunking and fact ranking helpers."""

from __future__ import annotations

from org_memory.services.chunking import (
    CHILD_TARGET_TOKENS,
    chunk_document,
    chunk_text,
    count_tokens,
)
from org_memory.services.ranking import rrf_fuse


def test_chunk_text_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []
    assert chunk_document("") == []


def test_chunk_text_prefixes_title() -> None:
    chunks = chunk_text("hello world " * 20, title="Doc Title")
    assert chunks
    assert chunks[0].text.startswith("Doc Title\n")


def test_chunk_document_links_children_to_parents() -> None:
    body = "\n\n".join(
        f"Paragraph number {i} with enough filler words here to grow the window. " * 12
        for i in range(40)
    )
    parents = chunk_document(body, title="Spec")
    assert parents
    assert all(p.children for p in parents)
    children = chunk_text(body, title="Spec")
    assert [c.index for c in children] == list(range(len(children)))
    assert all(c.parent_index >= 0 for c in children)
    for parent in parents:
        for child in parent.children:
            assert child.parent_index == parent.index
            assert count_tokens(child.text) <= CHILD_TARGET_TOKENS + 80


def test_chunk_windows_overlap() -> None:
    body = "\n\n".join(
        f"Paragraph number {i} with enough filler words here to grow the window. " * 8
        for i in range(20)
    )
    chunks = chunk_text(body)
    assert len(chunks) >= 2
    tail = chunks[0].text[-120:]
    assert any(tail[i : i + 40] in chunks[1].text for i in range(0, 80, 10))


def test_rrf_empty_lists() -> None:
    assert rrf_fuse([], k=60) == {}
