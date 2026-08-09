"""Sealed-PII envelope format (Pillar C1) — PURE, no key material, no I/O.

A field sealed client-side with HPKE is stored as a self-describing envelope so
the wire format survives key rotation and future suite additions. This module
only parses/serializes/validates the STRUCTURE — it never opens a ciphertext
(that needs the private key; see usali.opener).

Suite: DHKEM(P-256, HKDF-SHA256) + HKDF-SHA256 + AES-256-GCM. `enc` is the HPKE
encapsulated key = a P-256 uncompressed point (SEC1 0x04 || X || Y = 65 bytes).
"""

import base64
import json
from dataclasses import dataclass

SUITE_LABEL = "DHKEM-P256-HKDF-SHA256/HKDF-SHA256/AES-256-GCM"
_P256_UNCOMPRESSED_POINT_LEN = 65
_SEC1_UNCOMPRESSED_PREFIX = 0x04
ENVELOPE_VERSION = 1


class EnvelopeError(ValueError):
    """A sealed envelope is structurally invalid. Raised WITHOUT opening it."""


@dataclass(frozen=True)
class SealedEnvelope:
    version: int
    suite: str
    key_id: str
    enc: bytes
    ciphertext: bytes

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "suite": self.suite,
                "key_id": self.key_id,
                "enc": base64.b64encode(self.enc).decode("ascii"),
                "ct": base64.b64encode(self.ciphertext).decode("ascii"),
            },
            separators=(",", ":"),
        )

    @classmethod
    def parse(cls, raw: str) -> "SealedEnvelope":
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise EnvelopeError("sealed envelope is not valid JSON") from exc
        if not isinstance(d, dict):
            raise EnvelopeError("sealed envelope must be a JSON object")
        if d.get("version") != ENVELOPE_VERSION:
            raise EnvelopeError(f"unsupported envelope version {d.get('version')!r}")
        if d.get("suite") != SUITE_LABEL:
            raise EnvelopeError(f"unsupported suite {d.get('suite')!r}")
        key_id = d.get("key_id")
        if not isinstance(key_id, str) or not key_id:
            raise EnvelopeError("envelope key_id must be a non-empty string")
        try:
            enc = base64.b64decode(d["enc"], validate=True)
            ct = base64.b64decode(d["ct"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise EnvelopeError("envelope enc/ct are not valid base64") from exc
        if len(enc) != _P256_UNCOMPRESSED_POINT_LEN:
            raise EnvelopeError(
                f"envelope enc must be a {_P256_UNCOMPRESSED_POINT_LEN}-byte P-256 point"
            )
        if enc[0] != _SEC1_UNCOMPRESSED_PREFIX:
            raise EnvelopeError(
                "envelope enc must be a SEC1 uncompressed point (0x04 prefix)"
            )
        if not ct:
            raise EnvelopeError("envelope ct is empty")
        return cls(
            version=ENVELOPE_VERSION,
            suite=SUITE_LABEL,
            key_id=key_id,
            enc=enc,
            ciphertext=ct,
        )
