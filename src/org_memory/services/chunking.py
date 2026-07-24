"""Split document text into parent sections and searchable child chunks.

Children (~200 tokens) are embedded and ranked. Parents (~800 tokens) are
stored unembedded; retrieval returns parent text for a matched child.
Uses the embedding model's tokenizer (cl100k_base) for size and overlap.
"""

from __future__ import annotations

from dataclasses import dataclass

import tiktoken

# Child windows for embed/search.
CHILD_TARGET_TOKENS = 200
CHILD_OVERLAP_TOKENS = 40
CHILD_MAX_TOKENS = 320
# Parent sections returned to the agent after a child hit.
PARENT_TARGET_TOKENS = 800
PARENT_MAX_TOKENS = 1200

_ENCODING_NAME = "cl100k_base"


@dataclass(frozen=True)
class ChildChunk:
    index: int
    text: str
    parent_index: int


@dataclass(frozen=True)
class ParentChunk:
    index: int
    text: str
    children: tuple[ChildChunk, ...]


def _encoding():
    return tiktoken.get_encoding(_ENCODING_NAME)


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_encoding().encode(text))


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in text.split("\n\n")]
    return [p for p in parts if p]


def _token_len(pieces: list[str]) -> int:
    if not pieces:
        return 0
    return count_tokens("\n\n".join(pieces))


def _hard_wrap_tokens(paragraph: str, target: int, overlap: int, max_tokens: int) -> list[str]:
    """Word-wrap an oversized paragraph using token budgets."""
    enc = _encoding()
    words = paragraph.split()
    if not words:
        return []
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        trial = current + [word]
        trial_text = " ".join(trial)
        n = len(enc.encode(trial_text))
        if current and n > target:
            piece = " ".join(current)
            pieces.append(piece)
            if overlap > 0:
                token_ids = enc.encode(piece)
                keep = token_ids[-overlap:] if len(token_ids) > overlap else token_ids
                tail = enc.decode(keep).split()
                current = list(tail)
            else:
                current = []
            current.append(word)
            if len(enc.encode(" ".join(current))) > max_tokens:
                pieces.append(" ".join(current))
                current = []
        else:
            current = trial
            if n >= max_tokens:
                pieces.append(" ".join(current))
                current = []
    if current:
        joined = " ".join(current)
        if not pieces or joined != pieces[-1]:
            pieces.append(joined)
    return pieces


def _pack_paragraphs(
    paragraphs: list[str],
    *,
    target: int,
    overlap: int,
    max_tokens: int,
) -> list[str]:
    windows: list[str] = []
    current: list[str] = []
    for para in paragraphs:
        pieces = (
            _hard_wrap_tokens(para, target, overlap, max_tokens)
            if count_tokens(para) > max_tokens
            else [para]
        )
        for piece in pieces:
            if current and _token_len(current) + count_tokens(piece) + 2 > target:
                window = "\n\n".join(current)
                windows.append(window)
                if overlap > 0:
                    enc = _encoding()
                    token_ids = enc.encode(window)
                    keep = token_ids[-overlap:] if len(token_ids) > overlap else token_ids
                    overlap_text = enc.decode(keep).strip()
                    current = [overlap_text] if overlap_text else []
                else:
                    current = []
            current.append(piece)
    if current:
        windows.append("\n\n".join(current))
    return windows


def chunk_document(text: str, title: str = "") -> list[ParentChunk]:
    """Build parent sections with child windows. Empty input yields no chunks."""
    text = text.strip()
    if not text:
        return []

    paragraphs = _split_paragraphs(text)
    parent_texts = _pack_paragraphs(
        paragraphs,
        target=PARENT_TARGET_TOKENS,
        overlap=0,
        max_tokens=PARENT_MAX_TOKENS,
    )
    prefix = f"{title.strip()}\n\n" if title.strip() else ""
    parents: list[ParentChunk] = []
    child_index = 0
    for parent_index, parent_body in enumerate(parent_texts):
        child_bodies = _pack_paragraphs(
            _split_paragraphs(parent_body) or [parent_body],
            target=CHILD_TARGET_TOKENS,
            overlap=CHILD_OVERLAP_TOKENS,
            max_tokens=CHILD_MAX_TOKENS,
        )
        children: list[ChildChunk] = []
        for body in child_bodies:
            children.append(
                ChildChunk(
                    index=child_index,
                    text=f"{prefix}{body}",
                    parent_index=parent_index,
                )
            )
            child_index += 1
        parents.append(
            ParentChunk(
                index=parent_index,
                text=parent_body,
                children=tuple(children),
            )
        )
    return parents


def chunk_text(text: str, title: str = "") -> list[ChildChunk]:
    """Flatten children for callers that only need embed/search windows."""
    children: list[ChildChunk] = []
    for parent in chunk_document(text, title=title):
        children.extend(parent.children)
    return children
