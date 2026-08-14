"""Shared fixtures for DB-backed tests. Skips gracefully if DATABASE_URL is unset."""

from __future__ import annotations

import os

import pytest

from messjar.store import Store


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    return url


@pytest.fixture()
def store(database_url: str) -> Store:
    s = Store(database_url, min_size=1, max_size=2)
    yield s
    s.close()
