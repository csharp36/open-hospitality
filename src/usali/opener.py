"""The HPKE Opener seam (Pillar C1).

`SoftwareOpener` holds the recipient PRIVATE key in-process — dev/test ONLY. In
prod an `HsmOpener` (a deploy-time drop-in against this Protocol, deliberately not
built in C1, exactly like S3PhotoStore) keeps the private key in an HSM/KMS.

Suite: DHKEM(P-256, HKDF-SHA256) + HKDF-SHA256 + AES-256-GCM (see pii_crypto).
info binds the application context; the caller's aad binds employee_id:field.

pyhpke API (v0.6.4) used here:
  CipherSuite.new(kem_id, kdf_id, aead_id)
  KEMKey.from_pyca_cryptography_key(<cryptography EC key>)   # NOTE: *_key suffix
  enc, sender = suite.create_sender_context(pkr, info=...)
  recipient = suite.create_recipient_context(enc, skr, info=...)
  sender.seal(pt, aad=...) / recipient.open(ct, aad=...)
"""

import base64
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_private_key,
)
from pyhpke import AEADId, CipherSuite, KDFId, KEMId, KEMKey

from usali.pii_crypto import ENVELOPE_VERSION, SUITE_LABEL, SealedEnvelope

if TYPE_CHECKING:
    from usali.config import Settings

_LOG = logging.getLogger(__name__)

_INFO = b"usali-payroll-pii/v1"
_SUITE = CipherSuite.new(
    KEMId.DHKEM_P256_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES256_GCM
)


@dataclass(frozen=True)
class RecipientKey:
    key_id: str
    suite: str
    public_key: bytes  # SEC1 uncompressed point (65 bytes)


class Opener(Protocol):
    def public_key(self, key_id: str | None = None) -> RecipientKey: ...
    def open(self, envelope: SealedEnvelope, *, aad: bytes) -> bytes: ...
    def reseal(
        self, envelope: SealedEnvelope, recipient_public_key: bytes, *, aad: bytes
    ) -> SealedEnvelope: ...


def _pub_bytes(private: ec.EllipticCurvePrivateKey) -> bytes:
    return private.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )


def _seal(
    recipient_public_key: bytes, plaintext: bytes, *, aad: bytes, key_id: str
) -> SealedEnvelope:
    pkr = KEMKey.from_pyca_cryptography_key(
        ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), recipient_public_key
        )
    )
    enc, sender = _SUITE.create_sender_context(pkr, info=_INFO)
    ct = sender.seal(plaintext, aad=aad)
    return SealedEnvelope(
        version=ENVELOPE_VERSION,
        suite=SUITE_LABEL,
        key_id=key_id,
        enc=enc,
        ciphertext=ct,
    )


class SoftwareOpener:
    """In-process private key. DEV/TEST ONLY."""

    def __init__(
        self, private_key: ec.EllipticCurvePrivateKey, *, key_id: str
    ) -> None:
        self._private = private_key
        self._key_id = key_id
        self._kem_sk = KEMKey.from_pyca_cryptography_key(private_key)

    @classmethod
    def generate(cls, *, key_id: str) -> "SoftwareOpener":
        return cls(ec.generate_private_key(ec.SECP256R1()), key_id=key_id)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "SoftwareOpener":
        """Build from `pii_hpke_private_key` (base64 PKCS8) + `pii_hpke_key_id`.

        If no dev key is configured, generate an ephemeral one and log loudly —
        sealed values would not survive a restart, which is fine for dev/test but
        must never be how prod runs (create_app refuses the SoftwareOpener path in
        prod entirely).
        """
        if not settings.pii_hpke_private_key:
            _LOG.warning(
                "no pii_hpke_private_key configured; generating an EPHEMERAL dev "
                "HPKE key (key_id=%s) — sealed PII will not survive a restart",
                settings.pii_hpke_key_id,
            )
            return cls.generate(key_id=settings.pii_hpke_key_id)
        der = base64.b64decode(settings.pii_hpke_private_key, validate=True)
        private = load_der_private_key(der, password=None)
        if not isinstance(private, ec.EllipticCurvePrivateKey):
            raise ValueError("pii_hpke_private_key is not an EC (P-256) private key")
        return cls(private, key_id=settings.pii_hpke_key_id)

    def public_key(self, key_id: str | None = None) -> RecipientKey:
        return RecipientKey(
            key_id=self._key_id,
            suite=SUITE_LABEL,
            public_key=_pub_bytes(self._private),
        )

    def open(self, envelope: SealedEnvelope, *, aad: bytes) -> bytes:
        recipient = _SUITE.create_recipient_context(
            envelope.enc, self._kem_sk, info=_INFO
        )
        return recipient.open(envelope.ciphertext, aad=aad)

    def reseal(
        self, envelope: SealedEnvelope, recipient_public_key: bytes, *, aad: bytes
    ) -> SealedEnvelope:
        plaintext = self.open(envelope, aad=aad)
        return _seal(recipient_public_key, plaintext, aad=aad, key_id=self._key_id)


def seal_for_test(
    recipient: RecipientKey, plaintext: bytes, *, aad: bytes
) -> SealedEnvelope:
    """Simulate the browser client sealing a field. Tests + the interop fixture."""
    return _seal(recipient.public_key, plaintext, aad=aad, key_id=recipient.key_id)
