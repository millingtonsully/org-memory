"""Reserved identity namespaces for cross-system person linking."""

from __future__ import annotations

# Host platform User UUID (same UUID as principal user:<uuid>).
# ChangeEnvelope: identifiers[{namespace:"platform_user", value:"<uuid>", verified:true}]
# → PersonAlias.source_system = identity:platform_user
PLATFORM_USER_NAMESPACE = "platform_user"
PLATFORM_USER_SOURCE_SYSTEM = f"identity:{PLATFORM_USER_NAMESPACE}"
