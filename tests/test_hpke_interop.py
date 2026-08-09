"""Cross-library HPKE interop — the crux of Pillar C1.

Proves the SERVER crypto library (pyhpke, via ``SoftwareOpener``) can open an
envelope the BROWSER crypto library (``@hpke/core``) sealed. This is the exact
production path (browser seals -> DB stores the opaque envelope -> HSM opens at
provider-send). If the two libraries ever disagree on the suite, the ``info``
string, the AAD bytes, or the ``enc``/public-key point format, production would
silently store SSNs it can never decrypt — so this test must never be weakened
or skipped to get green. If it fails, the wire contract is wrong, not the test.

The fixture is committed (``tests/fixtures/hpke_interop.json``) and regenerated
by ``frontend/scripts/gen_hpke_fixture.mjs`` (which seals with ``@hpke/core``).
Its JS counterpart is ``frontend/src/lib/piiSeal.test.ts``.
"""

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_private_key

from usali.opener import SoftwareOpener
from usali.pii_crypto import SUITE_LABEL, SealedEnvelope

_FIXTURE = Path(__file__).parent / "fixtures" / "hpke_interop.json"


def _load_fixture() -> dict:
    return json.loads(_FIXTURE.read_text())


def _opener_from_fixture(data: dict) -> SoftwareOpener:
    der = base64.b64decode(data["private_key_pkcs8_b64"])
    private = load_der_private_key(der, password=None)
    assert isinstance(private, ec.EllipticCurvePrivateKey)
    return SoftwareOpener(private, key_id=data["envelope"]["key_id"])


def test_pyhpke_opens_a_browser_sealed_envelope() -> None:
    """pyhpke MUST open the @hpke/core-sealed ciphertext, byte-for-byte."""
    data = _load_fixture()
    opener = _opener_from_fixture(data)
    env = SealedEnvelope.parse(json.dumps(data["envelope"]))
    plaintext = opener.open(env, aad=data["aad"].encode())
    assert plaintext == data["plaintext"].encode()


def test_fixture_pins_the_exact_wire_contract() -> None:
    """The committed envelope matches the non-negotiable C1 contract exactly:
    the SealedEnvelope suite label, a 65-byte SEC1 enc point (0x04-prefixed),
    and the fixed application info + AAD strings.
    """
    data = _load_fixture()
    env = SealedEnvelope.parse(json.dumps(data["envelope"]))
    assert env.suite == SUITE_LABEL
    assert len(env.enc) == 65 and env.enc[0] == 0x04
    assert data["info"] == "usali-payroll-pii/v1"
    assert data["aad"] == "7:ssn"
    # The recipient public key is a 65-byte SEC1 uncompressed point too.
    assert len(base64.b64decode(data["public_key_sec1_b64"])) == 65


def test_open_with_the_wrong_aad_fails() -> None:
    """The AAD binds employee_id:field; opening the browser seal under a
    different field binding must fail authentication (the guarantee that a
    sealed blob cannot be moved between fields/employees)."""
    import pytest

    data = _load_fixture()
    opener = _opener_from_fixture(data)
    env = SealedEnvelope.parse(json.dumps(data["envelope"]))
    with pytest.raises(Exception):
        opener.open(env, aad=b"7:bank_account")
