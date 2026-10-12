import base64
import os

import pytest

from app.db import Database
from app.oauth.vault import Vault, VaultError


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'vault.db'}")
    database.create_all()
    return database


def test_round_trip_and_rotation(db):
    vault = Vault(db, os.urandom(32))
    vault.put_credential("t1", "c1", "access_token", "secret-1")
    assert vault.get_credential("t1", "c1", "access_token") == ("secret-1", None)
    vault.put_credential("t1", "c1", "access_token", "secret-2")
    assert vault.get_credential("t1", "c1", "access_token")[0] == "secret-2"
    assert vault.get_credential("t1", "c1", "refresh_token") is None
    assert vault.delete_credentials("c1") == 1
    assert vault.get_credential("t1", "c1", "access_token") is None


def test_tenant_keys_are_isolated(db):
    vault = Vault(db, os.urandom(32))
    vault.put_credential("tenant-a", "conn", "access_token", "for-a")
    # The same row read under another tenant's key must not decrypt.
    with pytest.raises(VaultError):
        vault.get_credential("tenant-b", "conn", "access_token")


def test_wrong_master_key_cannot_unwrap(db, tmp_path):
    key = os.urandom(32)
    Vault(db, key).put_credential("t", "c", "access_token", "v")
    other = Vault(db, os.urandom(32))
    with pytest.raises(VaultError):
        other.get_credential("t", "c", "access_token")


def test_platform_secret_binds_name(db):
    vault = Vault(db, os.urandom(32))
    blob = vault.encrypt_platform("oauth-app:gusto", "s3cret")
    assert vault.decrypt_platform("oauth-app:gusto", blob) == "s3cret"
    with pytest.raises(VaultError):
        vault.decrypt_platform("oauth-app:other", blob)


def test_from_settings_accepts_base64_and_rejects_bad_length(db):
    good = base64.b64encode(os.urandom(32)).decode()
    assert Vault.from_settings(db, good)
    with pytest.raises(VaultError):
        Vault(db, b"short")
    generated = Vault.generate_master_key()
    assert len(base64.b64decode(generated)) == 32
