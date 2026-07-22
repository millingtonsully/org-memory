"""Identity email normalize and merge corroboration gates."""

from __future__ import annotations

from org_memory.services.identity_merge import (
    corroborating_signals,
    has_sufficient_corroboration,
    normalize_email,
)


def test_normalize_email_strips_plus_and_gmail_dots() -> None:
    assert normalize_email("Alice.Smith+tag@Gmail.com") == "alicesmith@gmail.com"
    assert normalize_email("alice.smith@googlemail.com") == "alicesmith@gmail.com"
    assert normalize_email("  Bob+news@Example.COM ") == "bob@example.com"


def test_corroboration_requires_name_and_email() -> None:
    assert has_sufficient_corroboration(
        ["shared_normalized_name", "shared_email_address"]
    )
    assert not has_sufficient_corroboration(
        ["shared_normalized_name", "shared_verified_email_domain"]
    )
    assert not has_sufficient_corroboration(["shared_email_address"])
    assert not has_sufficient_corroboration(
        ["shared_normalized_name", "very_high_identity_similarity"]
    )


def test_shared_email_signal_requires_verified() -> None:
    from types import SimpleNamespace

    person_a = SimpleNamespace(display_name="Ada Lovelace", identity_embedding=None)
    person_b = SimpleNamespace(display_name="Ada Lovelace", identity_embedding=None)
    aliases_a = [SimpleNamespace(display_name="Ada", email="ada@x.com", email_verified=False)]
    aliases_b = [SimpleNamespace(display_name="Ada", email="ada@x.com", email_verified=True)]
    signals = corroborating_signals(aliases_a, aliases_b, person_a, person_b, similarity=0.0)
    assert "shared_normalized_name" in signals
    assert "shared_email_address" not in signals
