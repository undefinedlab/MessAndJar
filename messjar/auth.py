"""Shared-password auth for the bus.

Both sides use the same Railway HTTPS URL + the same password.
Clients send: Authorization: Bearer <password>
         or: X-MessJar-Password: <password>
"""

from __future__ import annotations

import hmac
import os
import secrets


def configured_password() -> str | None:
    """Server shared secret. Empty/None means auth disabled (local only)."""
    for key in ("MESSJAR_PASSWORD", "MESSJAR_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def passwords_match(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def extract_password(
    authorization: str | None,
    x_messjar_password: str | None,
) -> str | None:
    if x_messjar_password:
        return x_messjar_password.strip()
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()
    return None


def generate_password() -> str:
    return secrets.token_urlsafe(24)
