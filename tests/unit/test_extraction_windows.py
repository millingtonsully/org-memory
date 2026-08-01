"""Unit tests for extraction window splitting."""

from __future__ import annotations

from org_memory.services.extraction_windows import (
    _WINDOW_OVERLAP_CHARS,
    _WINDOW_TARGET_CHARS,
    split_windows,
)


def test_split_windows_empty() -> None:
    assert split_windows("") == []
    assert split_windows("   ") == []


def test_split_windows_under_target_is_single() -> None:
    text = "short paragraph"
    assert split_windows(text) == [text]


def test_split_windows_overlap_across_paragraphs() -> None:
    para = "x" * 1000
    # Enough paragraphs to exceed the target so we get multiple windows.
    n = (_WINDOW_TARGET_CHARS // 1000) + 3
    text = "\n\n".join([para] * n)
    windows = split_windows(text)
    assert len(windows) >= 2
    # Second window starts with overlap from the first window's tail.
    assert windows[1].startswith(windows[0][-_WINDOW_OVERLAP_CHARS:])
