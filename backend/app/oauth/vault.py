"""Envelope-encrypted credential storage.

A master key (from VAULT_MASTER_KEY) wraps one random data key per tenant. Tenant credentials are
AES-GCM encrypted with the tenant's data key, with the tenant id and credential kind bound in as
associated data, so a ciphertext cannot be replayed for another tenant or another credential kind.
Platform-level secrets (the OAuth client secret per integration) are encrypted with the master key.
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from app.db import Database, as_utc, utcnow
from app.oauth.models import CredentialRow, TenantKeyRow

log = logging.getLogger(__name__)

_DEV_MASTER_KEY = b"self-writing-integrations-dev-k!"  # 32 bytes; development only


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, db: Database, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise VaultError("vault master key must be exactly 32 bytes")
        self.db = db
        self._master = AESGCM(master_key)
        self._tenant_keys: dict[str, AESGCM] = {}

    @classmethod
    def from_settings(cls, db: Database, master_key_b64: str | None) -> "Vault":
        if master_key_b64:
            try:
                key = base64.b64decode(master_key_b64)
            except ValueError as exc:
                raise VaultError("VAULT_MASTER_KEY is not valid base64") from exc
        else:
            log.warning("VAULT_MASTER_KEY is not set; using the fixed development key")
            key = _DEV_MASTER_KEY
        return cls(db, key)

    @staticmethod
    def generate_master_key() -> str:
        return base64.b64encode(os.urandom(32)).decode("ascii")

    # --- primitives ---------------------------------------------------------------------

    @staticmethod
    def _seal(cipher: AESGCM, plaintext: str, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + cipher.encrypt(nonce, plaintext.encode("utf-8"), aad)

    @staticmethod
    def _open(cipher: AESGCM, blob: bytes, aad: bytes) -> str:
        try:
            return cipher.decrypt(blob[:12], blob[12:], aad).decode("utf-8")
        except Exception as exc:  # InvalidTag and friends
            raise VaultError("credential could not be decrypted (wrong key or tampered data)") from exc

    def _tenant_cipher(self, tenant_id: str) -> AESGCM:
        cipher = self._tenant_keys.get(tenant_id)
        if cipher is not None:
            return cipher
        aad = f"tenant-key:{tenant_id}".encode()
        with self.db.session() as s:
            row = s.get(TenantKeyRow, tenant_id)
            if row is None:
                raw = os.urandom(32)
                row = TenantKeyRow(tenant_id=tenant_id, wrapped_key=self._seal(self._master, base64.b64encode(raw).decode(), aad))
                s.add(row)
                s.commit()
            else:
                raw = base64.b64decode(self._open(self._master, row.wrapped_key, aad))
        cipher = AESGCM(raw)
        self._tenant_keys[tenant_id] = cipher
        return cipher

    # --- platform secrets ---------------------------------------------------------------

    def encrypt_platform(self, name: str, plaintext: str) -> bytes:
        return self._seal(self._master, plaintext, f"platform:{name}".encode())

    def decrypt_platform(self, name: str, blob: bytes) -> str:
        return self._open(self._master, blob, f"platform:{name}".encode())

    # --- tenant credentials ---------------------------------------------------------------

    def put_credential(self, tenant_id: str, connection_id: str, kind: str, value: str, expires_at: datetime | None = None) -> None:
        blob = self._seal(self._tenant_cipher(tenant_id), value, f"{tenant_id}:{connection_id}:{kind}".encode())
        with self.db.session() as s:
            row = s.scalar(select(CredentialRow).where(CredentialRow.connection_id == connection_id, CredentialRow.kind == kind))
            if row is None:
                row = CredentialRow(connection_id=connection_id, kind=kind, ciphertext=blob, expires_at=expires_at)
                s.add(row)
            else:
                row.ciphertext = blob
                row.expires_at = expires_at
                row.rotated_at = utcnow()
            s.commit()

    def get_credential(self, tenant_id: str, connection_id: str, kind: str) -> tuple[str, datetime | None] | None:
        with self.db.session() as s:
            row = s.scalar(select(CredentialRow).where(CredentialRow.connection_id == connection_id, CredentialRow.kind == kind))
            if row is None:
                return None
            blob, expires_at = row.ciphertext, as_utc(row.expires_at)
        value = self._open(self._tenant_cipher(tenant_id), blob, f"{tenant_id}:{connection_id}:{kind}".encode())
        return value, expires_at

    def delete_credentials(self, connection_id: str) -> int:
        with self.db.session() as s:
            rows = list(s.scalars(select(CredentialRow).where(CredentialRow.connection_id == connection_id)))
            for row in rows:
                s.delete(row)
            s.commit()
            return len(rows)
