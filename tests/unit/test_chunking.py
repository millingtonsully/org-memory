"""Chunking and fact ranking helpers."""

from __future__ import annotations

from org_memory.services.chunking import chunk_text
from org_memory.services.ranking import rrf_fuse


def test_chunk_text_empty() -> None:
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunk_text_prefixes_title() -> None:
    chunks = chunk_text("hello world " * 20, title="Doc Title")
    assert chunks
    assert chunks[0].text.startswith("Doc Title\n")


def test_chunk_text_splits_long_body() -> None:
    body = ("paragraph one about widgets.\n\n" * 40) + ("word " * 400)
    chunks = chunk_text(body)
    assert len(chunks) >= 2
    assert [c.index for c in chunks] == list(range(len(chunks)))


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
