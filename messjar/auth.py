"""Auth for Mess&Jar.

Two levels:
- Admin: MESSJAR_PASSWORD — full bus access (optional)
- Jar password: set when creating a Jar — scoped to that jar (what you share)

Clients send: Authorization: Bearer <password>
         or: X-MessJar-Password: <password>
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass


@dataclass
class AuthContext:
    admin: bool = False
    jar_id: str | None = None
    jar_name: str | None = None
    open_bus: bool = False

    def allows_jar(self, jar_id: str, jar_name: str | None = None) -> bool:
        if self.open_bus or self.admin:
            return True
        if self.jar_id and self.jar_id == jar_id:
            return True
        if jar_name and self.jar_name and self.jar_name == jar_name:
            return True
        return False


def configured_password() -> str | None:
    for key in ("MESSJAR_PASSWORD", "MESSJAR_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value
    return None


def passwords_match(provided: str | None, expected: str) -> bool:
    if provided is None or expected is None:
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
    return secrets.token_urlsafe(18)
