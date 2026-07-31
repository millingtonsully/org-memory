"""Email normalization shared by identity matching and person lookups.

This lives in the domain layer because both repositories and services key
person aliases on the normalized form. Keeping one implementation here means
lookup and write paths can never disagree about what counts as the same
address.
"""

from __future__ import annotations


def normalize_email(value: str) -> str:
    """Return the canonical form of an email address for identity matching.

    Lowercases the whole address, trims whitespace, drops a ``+tag`` suffix
    from the local part, and applies Gmail's rules (dots in the local part are
    ignored and googlemail.com is the same mailbox as gmail.com). Non-Gmail
    domains keep their dots because most providers treat them as significant.
    """
    cleaned = value.strip().casefold()
    if "@" not in cleaned:
        return cleaned
    local, _, domain = cleaned.partition("@")
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"
