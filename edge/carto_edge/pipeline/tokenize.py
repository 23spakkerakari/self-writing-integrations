"""Tokenization: forms to HMAC tokens under every live key version (spec 5.4 step 6, 7.1, 8.4).

:class:`Tokenizer` turns one identifier-class value into the :class:`carto_schema.event.Identifier`
entries of a canonical event: one entry per form (:mod:`carto_edge.pipeline.forms`) per key in
the keyring, active key first, so events carry a token under every version inside the rotation
overlap window (spec 8.4 "dual-tokenize"). The ``raw`` form of each value also becomes a
:class:`carto_edge.pipeline.model.VaultEntry` for the reveal vault, under every live version so
a reveal works whichever token core holds.

``shape`` and ``len`` on an identifier describe the form value, never the record value. The
event cap (:data:`carto_schema.event.MAX_IDENTIFIERS_PER_EVENT`) is applied with a fixed
priority, active key first in field order, then previous keys, so a rotation never pushes out
the tokens the linker uses for new links. Nothing here logs; no exception carries a value.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from carto_common.crypto import Keyring, TokenKey
from carto_edge.pipeline.forms import FormValue, compute_forms, normalize
from carto_edge.pipeline.model import FieldClass, VaultEntry
from carto_schema.event import MAX_FIELD_NAME_LEN, MAX_IDENTIFIERS_PER_EVENT, Identifier
from carto_schema.forms import shape, token_domain

__all__ = ["RAW_FORM", "FieldInput", "TokenizedFields", "Tokenizer"]

RAW_FORM: Final = "raw"

FieldInput = tuple[str, FieldClass, Sequence[str], str]
"""``(field path, class, forms, value)``: one tokenize-policy field of one record."""


@dataclass(frozen=True, slots=True)
class TokenizedFields:
    """The identifiers of one event, their vault entries and how many entries the cap cut."""

    identifiers: list[Identifier]
    vault_entries: list[VaultEntry]
    truncated: int


class Tokenizer:
    """Forms and tokens for one tenant under a :class:`carto_common.crypto.Keyring`.

    ``repr`` shows the tenant and the key versions only.
    """

    __slots__ = ("_keyring", "_tenant_id")

    def __init__(self, keyring: Keyring, tenant_id: str) -> None:
        self._keyring = keyring
        self._tenant_id = tenant_id

    def __repr__(self) -> str:
        return f"Tokenizer(tenant_id={self._tenant_id!r}, key_versions={self.key_versions!r})"

    @property
    def keyring(self) -> Keyring:
        return self._keyring

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def key_versions(self) -> list[int]:
        """Versions tokens are computed under, active first."""
        return [key.version for key in self._keyring.tokenization_keys()]

    @staticmethod
    def _identifier(key: TokenKey, field_path: str, form_value: FormValue) -> Identifier:
        return Identifier(
            field=field_path[:MAX_FIELD_NAME_LEN],
            form=form_value.form,
            token=key.token(token_domain(form_value.form), form_value.value),
            shape=shape(form_value.value),
            len=len(form_value.value),
        )

    def identifiers_for(
        self,
        field_path: str,
        field_class: FieldClass,
        forms: Sequence[str],
        value: str,
        *,
        expires_at: datetime,
    ) -> tuple[list[Identifier], list[VaultEntry]]:
        """Identifiers of one field value under every live key, plus its vault entries.

        The identifiers are grouped by key, active first, each group in form order. A vault
        entry is produced for the ``raw`` form under every key; ``expires_at`` is the event
        retention deadline the caller computed (spec 14.10: vault entries follow events).
        """
        form_values = compute_forms(value, field_class, forms)
        if not form_values:
            return [], []
        identifiers: list[Identifier] = []
        entries: list[VaultEntry] = []
        for key in self._keyring.tokenization_keys():
            for form_value in form_values:
                identifier = self._identifier(key, field_path, form_value)
                identifiers.append(identifier)
                if form_value.form == RAW_FORM:
                    entries.append(VaultEntry(identifier.token, form_value.value, expires_at))
        return identifiers, entries

    def build_event_identifiers(
        self, entries: Iterable[FieldInput], *, expires_at: datetime
    ) -> TokenizedFields:
        """The identifier list of one event, capped at ``MAX_IDENTIFIERS_PER_EVENT``.

        Priority under the cap: every form of every field under the active key, in field
        order, then the previous keys in keyring order. A field path that appears twice keeps
        its first value (the event contract allows one token per field, form and version).
        Vault entries are produced only for ``raw`` tokens that made it into the event.
        """
        computed: list[tuple[str, list[FormValue]]] = []
        seen_fields: set[str] = set()
        for field_path, field_class, forms, value in entries:
            if field_path in seen_fields:
                continue
            form_values = compute_forms(value, field_class, forms)
            if not form_values:
                continue
            seen_fields.add(field_path)
            computed.append((field_path, form_values))

        identifiers: list[Identifier] = []
        vault_entries: list[VaultEntry] = []
        total = 0
        for key in self._keyring.tokenization_keys():
            for field_path, form_values in computed:
                for form_value in form_values:
                    total += 1
                    if len(identifiers) >= MAX_IDENTIFIERS_PER_EVENT:
                        continue
                    identifier = self._identifier(key, field_path, form_value)
                    identifiers.append(identifier)
                    if form_value.form == RAW_FORM:
                        vault_entries.append(
                            VaultEntry(identifier.token, form_value.value, expires_at)
                        )
        return TokenizedFields(identifiers, vault_entries, total - len(identifiers))

    def tokenize_query(self, value: str, forms: Sequence[str] | None = None) -> list[str]:
        """Tokens of a search value for every form under every key version (spec 12
        ``/internal/tokenize``, 14.10 targeted deletion). Identifier forms unless ``forms`` says
        otherwise; active key first."""
        form_values = compute_forms(value, FieldClass.IDENTIFIER, tuple(forms or ()))
        return [
            key.token(token_domain(form_value.form), form_value.value)
            for key in self._keyring.tokenization_keys()
            for form_value in form_values
        ]

    def actor_token(self, value: str) -> str:
        """The ``raw`` id-domain token of an actor value under the active key (spec 7.1
        ``actor``). Raises :class:`ValueError` for a value that normalizes to nothing."""
        raw = normalize(value)
        if not raw:
            msg = "actor value is empty after normalization"
            raise ValueError(msg)
        return self._keyring.active.token("id", raw)
