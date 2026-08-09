import base64

import pytest

from usali.crypto import EncryptedString, decrypt_str, encrypt_str


def test_round_trip():
    ct = encrypt_str("Social Security note: 123")
    assert ct != "Social Security note: 123"  # actually encrypted
    assert decrypt_str(ct) == "Social Security note: 123"


def test_ciphertext_is_nondeterministic():
    # Fresh 96-bit nonce per call → same plaintext, different ciphertext.
    assert encrypt_str("same") != encrypt_str("same")


def test_tampered_ciphertext_rejected():
    ct = encrypt_str("secret")
    raw = bytearray(base64.b64decode(ct))
    raw[-1] ^= 0x01  # flip a bit in the GCM tag
    tampered = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(Exception):  # cryptography raises InvalidTag
        decrypt_str(tampered)


def test_type_decorator_bind_and_result():
    t = EncryptedString()
    stored = t.process_bind_param("hello", dialect=None)
    assert stored is not None and stored != "hello"
    assert t.process_result_value(stored, dialect=None) == "hello"
    assert t.process_bind_param(None, dialect=None) is None
    assert t.process_result_value(None, dialect=None) is None


def test_encrypted_bytes_type_decorator_bind_and_result():
    """F1: face-template embeddings are raw bytes encrypted at rest with the
    same field key (server-readable — matching needs them in memory — NOT
    HPKE-sealed like payroll PII). A breach of unencrypted biometric data is
    CA's one private-right-of-action scenario, so at-rest encryption is the
    load-bearing property here."""
    from usali.crypto import EncryptedBytes

    t = EncryptedBytes()
    embedding = bytes(range(256))
    stored = t.process_bind_param(embedding, dialect=None)
    assert stored is not None and stored != embedding
    assert t.process_result_value(stored, dialect=None) == embedding
    assert t.process_bind_param(None, dialect=None) is None
    assert t.process_result_value(None, dialect=None) is None
