import base64
import json

import pytest

from usali.pii_crypto import SUITE_LABEL, EnvelopeError, SealedEnvelope


def _valid_json(enc_len=65, ct_len=32):
    return SealedEnvelope(
        version=1,
        suite=SUITE_LABEL,
        key_id="dev-1",
        enc=b"\x04" + b"\x11" * (enc_len - 1),
        ciphertext=b"\x22" * ct_len,
    ).to_json()


def test_round_trips():
    env = SealedEnvelope.parse(_valid_json())
    assert env.suite == SUITE_LABEL
    assert env.key_id == "dev-1"
    assert len(env.enc) == 65
    assert SealedEnvelope.parse(env.to_json()) == env


def test_rejects_bad_json():
    with pytest.raises(EnvelopeError):
        SealedEnvelope.parse("{not json")


def test_rejects_unknown_suite():
    d = json.loads(_valid_json())
    d["suite"] = "AES-128-CBC"
    with pytest.raises(EnvelopeError, match="suite"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_wrong_enc_length():
    # P-256 uncompressed point is exactly 65 bytes; anything else is malformed.
    d = json.loads(_valid_json())
    d["enc"] = base64.b64encode(b"\x04" + b"\x11" * 30).decode()
    with pytest.raises(EnvelopeError, match="enc"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_missing_key_id():
    d = json.loads(_valid_json())
    d["key_id"] = ""
    with pytest.raises(EnvelopeError, match="key_id"):
        SealedEnvelope.parse(json.dumps(d))


# --- Extra adversarial tests (added by executor; do not weaken the above) ---
# The validator must reject malformed input WITHOUT ever opening a ciphertext.
# This module is pure, so "not opening" is structural: none of these paths touch
# key material — they only inspect the envelope's shape.


def test_rejects_json_array_not_object():
    # A top-level JSON array (or any non-object) must be refused.
    with pytest.raises(EnvelopeError, match="object"):
        SealedEnvelope.parse(json.dumps([1, 2, 3]))


def test_rejects_json_scalar_not_object():
    with pytest.raises(EnvelopeError, match="object"):
        SealedEnvelope.parse(json.dumps("just-a-string"))


def test_rejects_base64_with_invalid_chars():
    # validate=True must reject non-alphabet characters in enc.
    d = json.loads(_valid_json())
    d["enc"] = "!!!!not-base64!!!!"
    with pytest.raises(EnvelopeError, match="base64"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_base64_with_bad_padding():
    # A base64 string whose length is not a multiple of 4 is malformed.
    d = json.loads(_valid_json())
    d["ct"] = "abc"  # 3 chars -> invalid base64 length
    with pytest.raises(EnvelopeError, match="base64"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_missing_enc_key():
    d = json.loads(_valid_json())
    del d["enc"]
    with pytest.raises(EnvelopeError, match="base64"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_missing_ct_key():
    d = json.loads(_valid_json())
    del d["ct"]
    with pytest.raises(EnvelopeError, match="base64"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_empty_ciphertext():
    # ct present but decodes to zero bytes -> nothing was sealed.
    d = json.loads(_valid_json())
    d["ct"] = ""
    with pytest.raises(EnvelopeError, match="ct"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_unsupported_version():
    d = json.loads(_valid_json())
    d["version"] = 2
    with pytest.raises(EnvelopeError, match="version"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_non_string_key_id():
    d = json.loads(_valid_json())
    d["key_id"] = 123
    with pytest.raises(EnvelopeError, match="key_id"):
        SealedEnvelope.parse(json.dumps(d))


def test_rejects_enc_of_65_bytes_not_starting_with_0x04():
    # A SEC1 uncompressed point MUST lead with 0x04. A 65-byte value with any
    # other prefix (e.g. 0x05 compressed/hybrid) is structurally not our point
    # and is refused here — still WITHOUT opening the ciphertext.
    d = json.loads(_valid_json())
    d["enc"] = base64.b64encode(b"\x05" + b"\x11" * 64).decode()
    with pytest.raises(EnvelopeError, match="enc"):
        SealedEnvelope.parse(json.dumps(d))
