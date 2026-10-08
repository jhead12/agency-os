"""
Accounts' own enricher keys (core/credentials.py): encrypted at rest, bound to
their account and provider, write-only through /api/v1, and re-encrypted when
the master key is rotated.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_credentials.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import accounts, credentials  # noqa: E402
from tests.test_access import client_for, db  # noqa: E402,F401

SECRET = "apollo_live_1234567890abcd"


@pytest.fixture
def master(monkeypatch):
    key = credentials.new_master_key()
    monkeypatch.setenv(credentials.KEY_ENV, key)
    monkeypatch.delenv(credentials.OLD_KEYS_ENV, raising=False)
    return key


@pytest.fixture
def account(db, master):
    row, key = accounts.create(db, "org_1", "Acme")
    return {"row": row, "headers": {"Authorization": f"Bearer {key}"}}


def test_a_saved_key_is_encrypted_and_only_reveal_returns_it(db, account):
    aid = account["row"]["id"]
    credentials.put(db, aid, "apollo", SECRET)
    stored = db.conn.execute("SELECT * FROM account_credentials").fetchone()
    assert SECRET not in stored["secret"] and stored["hint"] == "abcd"
    assert credentials.reveal(db, aid, "apollo") == SECRET
    assert credentials.reveal(db, aid, "hunter") is None
    audit = db.conn.execute("SELECT details FROM audit_log WHERE action = 'account.credential_set'").fetchone()
    assert SECRET not in audit["details"]


def test_a_row_moved_to_another_account_or_provider_does_not_decrypt(db, account):
    aid = account["row"]["id"]
    other, _ = accounts.create(db, "org_2", "Other")
    credentials.put(db, aid, "apollo", SECRET)
    db.conn.execute("UPDATE account_credentials SET account_id = ?", (other["id"],))
    db.conn.commit()
    with pytest.raises(credentials.CredentialError):
        credentials.reveal(db, other["id"], "apollo")
    db.conn.execute("UPDATE account_credentials SET account_id = ?, provider = 'hunter'", (aid,))
    db.conn.commit()
    with pytest.raises(credentials.CredentialError):
        credentials.reveal(db, aid, "hunter")


def test_without_the_master_key_nothing_decrypts(db, account, monkeypatch):
    credentials.put(db, account["row"]["id"], "apollo", SECRET)
    monkeypatch.setenv(credentials.KEY_ENV, credentials.new_master_key())
    with pytest.raises(credentials.NotConfigured):
        credentials.reveal(db, account["row"]["id"], "apollo")


def test_rotating_the_master_key(db, account, master, monkeypatch):
    aid = account["row"]["id"]
    credentials.put(db, aid, "apollo", SECRET)
    monkeypatch.setenv(credentials.KEY_ENV, credentials.new_master_key())
    monkeypatch.setenv(credentials.OLD_KEYS_ENV, master)
    assert credentials.reveal(db, aid, "apollo") == SECRET
    assert credentials.reencrypt(db) == 1
    assert credentials.reencrypt(db) == 0
    monkeypatch.delenv(credentials.OLD_KEYS_ENV)
    assert credentials.reveal(db, aid, "apollo") == SECRET


def test_bad_input_is_refused(db, account):
    aid = account["row"]["id"]
    with pytest.raises(credentials.CredentialError):
        credentials.put(db, aid, "salesforce", SECRET)
    for bad in ("", "short", "has a space in it", "x" * 513):
        with pytest.raises(credentials.CredentialError):
            credentials.put(db, aid, "apollo", bad)


def test_api_is_write_only(db, account):
    c, h = client_for(), account["headers"]
    r = c.put("/api/v1/account/credentials/apollo", json={"key": SECRET}, headers=h)
    assert r.status_code == 200 and r.json()["credential"]["hint"] == "abcd"
    assert SECRET not in r.text
    listed = c.get("/api/v1/account/credentials", headers=h)
    assert [x["provider"] for x in listed.json()["credentials"]] == ["apollo"] and SECRET not in listed.text
    assert c.put("/api/v1/account/credentials/nope", json={"key": SECRET}, headers=h).status_code == 422
    assert c.delete("/api/v1/account/credentials/apollo", headers=h).status_code == 204
    assert c.delete("/api/v1/account/credentials/apollo", headers=h).status_code == 404


def test_api_scopes_keys_to_the_calling_account(db, account):
    c = client_for()
    c.put("/api/v1/account/credentials/apollo", json={"key": SECRET}, headers=account["headers"])
    _, other_key = accounts.create(db, "org_2", "Other")
    other = {"Authorization": f"Bearer {other_key}"}
    assert c.get("/api/v1/account/credentials", headers=other).json()["credentials"] == []
    assert c.delete("/api/v1/account/credentials/apollo", headers=other).status_code == 404
    assert c.get("/api/v1/account/credentials").status_code == 401


def test_api_refuses_to_save_without_a_master_key(db, account, monkeypatch):
    monkeypatch.delenv(credentials.KEY_ENV)
    r = client_for().put("/api/v1/account/credentials/apollo", json={"key": SECRET}, headers=account["headers"])
    assert r.status_code == 503
