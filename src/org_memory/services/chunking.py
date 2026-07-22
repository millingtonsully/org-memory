"""Split document text into searchable child chunks.

The parent is the full document; children are 225-token windows for embed and
search. Uses chars/4 for size. Output is deterministic for idempotent replay.
"""

from __future__ import annotations

from dataclasses import dataclass

# 225 tokens at chars/4.
CHILD_TARGET_CHARS = 900
# Overlap so text near a boundary appears in both chunks.
CHILD_OVERLAP_CHARS = 150
# Max size for one unbroken paragraph.
CHILD_MAX_CHARS = 1400


@dataclass(frozen=True)
class ChildChunk:
    index: int
    text: str


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines."""
    parts = [p.strip() for p in text.split("\n\n")]
    return [p for p in parts if p]


def _hard_wrap(paragraph: str) -> list[str]:
    """Word-wrap an oversized paragraph with overlap."""
    words = paragraph.split()
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        current.append(word)
        length += len(word) + 1
        if length >= CHILD_TARGET_CHARS:
            pieces.append(" ".join(current))
            tail: list[str] = []
            tail_len = 0
            for w in reversed(current):
                tail_len += len(w) + 1
                if tail_len > CHILD_OVERLAP_CHARS:
                    break
                tail.insert(0, w)
            current = tail.copy()
            length = sum(len(w) + 1 for w in current)
    if current and (not pieces or " ".join(current) != pieces[-1]):
        pieces.append(" ".join(current))
    return pieces


def chunk_text(text: str, title: str = "") -> list[ChildChunk]:
    """Build child chunks from paragraphs, prefixing the title on each."""
    text = text.strip()
    if not text:
        return []

    windows: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for para in _split_paragraphs(text):
        pieces = _hard_wrap(para) if len(para) > CHILD_MAX_CHARS else [para]
        for piece in pieces:
            if current_parts and current_len + len(piece) + 2 > CHILD_TARGET_CHARS:
                window = "\n\n".join(current_parts)
                windows.append(window)
                overlap = window[-CHILD_OVERLAP_CHARS:] if len(window) > CHILD_OVERLAP_CHARS else window
                current_parts = [overlap] if overlap.strip() else []
                current_len = len(overlap) + 2 if current_parts else 0
            current_parts.append(piece)
            current_len += len(piece) + 2
    if current_parts:
        windows.append("\n\n".join(current_parts))

    prefix = f"{title.strip()}\n\n" if title.strip() else ""
    return [ChildChunk(index=i, text=f"{prefix}{w}") for i, w in enumerate(windows)]
