"""Planting credential rows the application cannot read (OH-17 Task 12).

ADR-005 records the failure these helpers reproduce: rotating
`field_encryption_key` leaves every stored ciphertext structurally perfect and
undecryptable, because there is no envelope and no key version to fall back
on. `integrations.CredentialUnreadable` is what a tenant meets when that has
happened, and it is asserted from four test modules — the resolver unit tests
plus the three consumer surfaces — so the way the row is planted lives in ONE
place. Four hand-rolled INSERTs would drift into four different ideas of what
"unreadable" means, and the interesting one (right shape, wrong key) is the
one a hurried copy gets wrong.
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.orm import Session

from usali.tenancy import FOUNDING_ORG_ID


def unreadable_ciphertext(plaintext: str = "s3cret") -> str:
    """Ciphertext in EXACTLY `crypto.EncryptedString`'s wire shape —
    base64(nonce || ct || tag), AES-256-GCM — sealed under a key this
    deployment does not hold.

    THE ADR-005 case: after a key rotation the column still decodes as base64
    and still has the right structure; only the GCM tag says no, and
    `decrypt_str` raises `InvalidTag`. A helper that planted obvious garbage
    instead would prove the base64 decoder refuses it and nothing about the
    rotation."""
    key = AESGCM.generate_key(bit_length=256)  # NOT the field encryption key
    nonce = os.urandom(12)
    return base64.b64encode(
        nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    ).decode("ascii")


def plant_credential(
    session: Session, integration: str, provider: str, **columns: str
) -> None:
    """INSERT one credential row for the FOUNDING ORG through `text()`, so
    `EncryptedString`'s bind processor never runs and every value lands on
    disk exactly as given. Commits, because the readers under test open their
    own sessions.

    The ORM path cannot plant this row: it would helpfully encrypt the
    ciphertext under the CURRENT key, and the row would then be perfectly
    readable — a test that looked identical and asserted nothing.

    `org_id` is explicit and scoped to org 1: every consumer world here is the
    founding org, and an org-scoped write carrying no org_id is confined by
    RLS alone (which the superuser test connection bypasses)."""
    cols = ", ".join(columns)
    placeholders = ", ".join(f":{name}" for name in columns)
    session.execute(
        text(
            f"INSERT INTO org_integration_credential "  # noqa: S608
            f"(org_id, integration, provider, connected_by, {cols}) VALUES "
            f"(:org_id, :integration, :provider, 'test-subject', {placeholders})"
        ),
        {
            "org_id": FOUNDING_ORG_ID,
            "integration": integration,
            "provider": provider,
            **columns,
        },
    )
    session.commit()
