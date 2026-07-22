from __future__ import annotations

import re

PRINCIPAL_PATTERN = re.compile(
    r"^(user|group):"
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)


def is_valid_principal(value: str) -> bool:
    return bool(PRINCIPAL_PATTERN.fullmatch(value))


def require_principal(value: str, *, field: str = "principal") -> str:
    """Return value if valid; raise ValueError with an actionable message otherwise."""
    cleaned = value.strip()
    if not is_valid_principal(cleaned):
        raise ValueError(
            f"{field} must be 'user:<uuid>' or 'group:<uuid>' (platform User / "
            f"PermissionGroup id); got {value!r}"
        )
    return cleaned
