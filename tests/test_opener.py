import pytest

from usali.opener import SoftwareOpener, seal_for_test
from usali.pii_crypto import SealedEnvelope


def _opener() -> SoftwareOpener:
    return SoftwareOpener.generate(key_id="dev-1")  # test convenience constructor


def test_seal_then_open_round_trips():
    op = _opener()
    pub = op.public_key()
    env = seal_for_test(pub, b"123-45-6789", aad=b"7:ssn")
    assert op.open(env, aad=b"7:ssn") == b"123-45-6789"


def test_open_with_wrong_aad_fails():
    op = _opener()
    env = seal_for_test(op.public_key(), b"secret", aad=b"7:ssn")
    with pytest.raises(Exception):  # AEAD authentication failure
        op.open(env, aad=b"7:bank_account")  # wrong field binding


def test_tampered_ciphertext_fails():
    op = _opener()
    env = seal_for_test(op.public_key(), b"secret", aad=b"7:ssn")
    bad = SealedEnvelope(
        version=env.version,
        suite=env.suite,
        key_id=env.key_id,
        enc=env.enc,
        ciphertext=env.ciphertext[:-1] + bytes([env.ciphertext[-1] ^ 1]),
    )
    with pytest.raises(Exception):
        op.open(bad, aad=b"7:ssn")


def test_public_key_reports_suite_and_key_id():
    op = _opener()
    pub = op.public_key()
    assert pub.key_id == "dev-1"
    assert len(pub.public_key) == 65  # P-256 uncompressed point


def test_reseal_moves_a_secret_between_keys():
    a, b = _opener(), SoftwareOpener.generate(key_id="dev-2")
    env = seal_for_test(a.public_key(), b"routing", aad=b"7:bank_routing")
    resealed = a.reseal(env, b.public_key().public_key, aad=b"7:bank_routing")
    assert b.open(resealed, aad=b"7:bank_routing") == b"routing"
