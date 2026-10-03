"""Shared tenant_id validation."""
import re

TENANT_ID_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")


def validate_tenant_id(tenant_id: str) -> str:
    """Raise ValueError if tenant_id is invalid; return it on success."""
    if not TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise ValueError(
            f"Invalid tenant_id (must match {TENANT_ID_PATTERN.pattern}): {tenant_id!r}"
        )
    return tenant_id
