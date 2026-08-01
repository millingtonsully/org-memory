"""Split document text into overlapping extraction windows.

Windows are much larger than retrieval chunks: extraction cost scales with
source length, and larger windows avoid repeating LLM overhead. Overlap keeps
facts that straddle a boundary visible in full at least once; duplicate
extractions are deduped downstream in the graph repositories.
"""

from __future__ import annotations

# ~32k chars, roughly 8k tokens on English prose.
_WINDOW_TARGET_CHARS = 32000
# Tail of each window repeated at the start of the next.
_WINDOW_OVERLAP_CHARS = 2000


def split_windows(text: str) -> list[str]:
    """Split text into paragraph-aligned extraction windows covering ALL of it.

    Windows target ``_WINDOW_TARGET_CHARS`` and carry ``_WINDOW_OVERLAP_CHARS`` of
    the previous window's tail so boundary-spanning facts are seen whole. A
    single paragraph longer than the target becomes its own window (never
    truncated).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= _WINDOW_TARGET_CHARS:
        return [text]

    paragraphs = [p for p in (part.strip() for part in text.split("\n\n")) if p]
    windows: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            windows.append("\n\n".join(current))
            current = []
            current_len = 0

    for para in paragraphs:
        if current and current_len + len(para) + 2 > _WINDOW_TARGET_CHARS:
            tail = "\n\n".join(current)
            flush()
            overlap = tail[-_WINDOW_OVERLAP_CHARS:]
            current = [overlap]
            current_len = len(overlap) + 2
        current.append(para)
        current_len += len(para) + 2
    flush()
    return windows
