"""
Shared fixtures. Database tests need a PostgreSQL database to wipe:

    TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/

They are skipped when TEST_DATABASE_URL isn't set.
"""

import os

# Never read the developer's .env in tests (core/env.py): real keys and owner
# settings would change what the tests see.
os.environ["AGENCY_OS_DOTENV"] = "off"

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from core.db import reset_schema_cache


@pytest.fixture
def pg_url(monkeypatch):
    """A freshly emptied test database; also set as DATABASE_URL."""
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    if "test" not in (conninfo_to_dict(url).get("dbname") or ""):
        pytest.fail("TEST_DATABASE_URL must name a database with 'test' in it; tests wipe it.")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    reset_schema_cache()
    monkeypatch.setenv("DATABASE_URL", url)
    return url
