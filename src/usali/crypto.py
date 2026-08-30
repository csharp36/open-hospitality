"""Field-level encryption for PII columns (Pillar A2.2).

`EncryptedString` is a SQLAlchemy TypeDecorator that transparently AES-256-GCM
encrypts a string on write and decrypts on read, storing base64(nonce||ct||tag)
as TEXT. The key is loaded from settings (`field_encryption_key`, base64 of 32
bytes). Pillar C adds real PII columns (SSN, bank) using this same type — the
protection is defined here, once.
"""

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import LargeBinary, Text
from sqlalchemy.types import TypeDecorator

from usali.config import get_settings

_NONCE_BYTES = 12  # 96-bit nonce, the AES-GCM standard

# The org whose photo key IS the raw master key (Pillar L decision 5). This
# is org 1 — the founding org (== usali.tenancy.FOUNDING_ORG_ID by
# construction; A2.1 seeds exactly one org). crypto cannot import tenancy
# (tenancy -> models -> crypto would cycle), so the value is repeated here
# with the cross-reference. Do NOT change it independently of tenancy.
_PHOTO_MASTER_ORG_ID = 1

# The HKDF info string for photo keys — a fixed domain-separation label so
# the photo key derivation can never collide with any future HKDF use of
# the same master. The org_id is the SALT (see _photo_key): dropping the
# salt collapses every org != 1 onto ONE key (info is fixed), which is the
# exact leak the per-org design exists to prevent — pinned in the two-org
# photo test.
_PHOTO_HKDF_INFO = b"usali-punch-photo-key-v1"

# Domain-separation label for the OAuth `state` signing key (OH-17,
# D-OH17.11). A fixed info string so this derivation can never collide with
# the photo keys' — same master, different purpose, and the labels are what
# keep them apart. Changing this string invalidates every state signed under
# the old one, which is harmless (they live 10 minutes) but is the only thing
# it does; it is not a rotation mechanism for the master key.
_OAUTH_STATE_HKDF_INFO = b"usali-integration-oauth-state-v1"


def _key() -> bytes:
    raw = base64.b64decode(get_settings().field_encryption_key)
    if len(raw) != 32:
        raise ValueError("field_encryption_key must decode to 32 bytes (AES-256)")
    return raw


def encrypt_str(plaintext: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_str(token: str) -> str:
    raw = base64.b64decode(token)
    nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    return AESGCM(_key()).decrypt(nonce, ct, None).decode("utf-8")


def encrypt_bytes(data: bytes) -> bytes:
    """AES-256-GCM encrypt raw bytes under the GLOBAL master key. Returns
    nonce || ct || tag. Used for the `EncryptedBytes` DB column (face-template
    embeddings). Photos take the PER-ORG path (`encrypt_photo`); this stays
    global on purpose — the DB PII/embedding columns are already RLS-org-
    confined and are explicitly out of scope for per-org re-keying (L5)."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_key()).encrypt(nonce, data, None)


def decrypt_bytes(blob: bytes) -> bytes:
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(_key()).decrypt(nonce, ct, None)


def _photo_key(org_id: int) -> bytes:
    """The AES-256-GCM data key for one org's punch/face photos (L5
    decision 5). Photos live OUTSIDE Postgres (GCS objects, local files)
    where the RLS wall cannot reach, so the tenant boundary lives in the
    CIPHERTEXT: each org's photos are sealed under a key only that org's
    prefix derives, so a prefix-routing bug yields UNDECRYPTABLE bytes, not
    another hotel's punch photos.

    Org 1 (the founding org) is DEFINED as the raw master key — no HKDF.
    Every photo written before L5 was sealed under the master key with an
    un-prefixed object key, and org 1 is the only org those objects can
    belong to (A2.1 seeds exactly one org); defining org 1's key as the
    master key is what makes them stay readable with NO re-encrypt window
    and no data migration. Derivation applies to org != 1 ONLY. Do NOT
    "simplify" org 1 into the HKDF branch — that silently re-keys every
    existing punch photo into unreadable ciphertext.

    org_id is the HKDF SALT (info is a fixed domain label): two distinct
    orgs derive two distinct keys, and dropping the salt collapses them
    onto one — the mutant the two-org photo pin kills."""
    # Coerce to int where the branch is DECIDED, never assumed of callers:
    # a stringified founding-org id ("1") must still take the master-key
    # branch, or it would silently HKDF-re-key every existing org-1 photo
    # into undecryptable ciphertext — the exact outcome this function warns
    # against. Pinned: _photo_key("1") == _photo_key(1) == master.
    org_id = int(org_id)
    master = _key()
    if org_id == _PHOTO_MASTER_ORG_ID:
        return master
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=str(org_id).encode("ascii"),
        info=_PHOTO_HKDF_INFO,
    ).derive(master)


def oauth_state_key() -> bytes:
    """The HMAC key protecting the integration OAuth `state` parameter
    (OH-17, D-OH17.11).

    HKDF-derived from the master field-encryption key rather than configured
    separately — the `_photo_key` precedent directly above: OH-17 introduces
    no new deployment secret, and production already fail-fasts on the
    committed dev-default master (`config._refuse_dev_secrets_in_prod`), so
    this key inherits that guard for free.

    Derived and NOT the master itself, which would be the tempting
    one-liner: `state` signing and PII decryption would then be the same
    compromise, and a forged state is a cross-tenant credential injection
    (`integrations_api.callback`). The fixed info label is what separates
    them; there is no salt because there is exactly one such key
    process-wide — unlike the photo keys, which are per-org and salt on the
    org id."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_OAUTH_STATE_HKDF_INFO,
    ).derive(_key())


def encrypt_photo(data: bytes, org_id: int) -> bytes:
    """AES-256-GCM encrypt a photo under `org_id`'s per-org key. Returns
    nonce || ct || tag, the same wire shape as `encrypt_bytes`."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_photo_key(org_id)).encrypt(nonce, data, None)


def decrypt_photo(blob: bytes, org_id: int) -> bytes:
    """Decrypt a photo sealed by `encrypt_photo` for `org_id`. A blob sealed
    for a DIFFERENT org fails the GCM tag (InvalidTag) — the intended
    failure mode of a cross-org prefix-routing bug."""
    nonce, ct = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(_photo_key(org_id)).decrypt(nonce, ct, None)


class EncryptedString(TypeDecorator[str]):
    """TEXT column whose value is AES-256-GCM encrypted at rest."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        return None if value is None else encrypt_str(value)

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        return None if value is None else decrypt_str(value)


class EncryptedBytes(TypeDecorator[bytes]):
    """BYTEA column whose value is AES-256-GCM encrypted at rest (F1: face
    template embeddings — server-readable under the field key, deliberately
    NOT HPKE-sealed, because matching needs the vector in memory)."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: bytes | None, dialect: object) -> bytes | None:
        return None if value is None else encrypt_bytes(value)

    def process_result_value(self, value: bytes | None, dialect: object) -> bytes | None:
        return None if value is None else decrypt_bytes(value)
