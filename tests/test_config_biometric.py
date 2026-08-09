"""F1: biometric matching ships DARK, and production can't turn it on
without the notice-at-collection document existing as a versioned artifact
(the Pillar F face-match design decision 7). Dev/demo enable
freely with synthetic people.
"""

import pytest

from pathlib import Path

from usali.config import Settings

_REAL_KEY = "bm90LWEtcmVhbC1rZXktYnV0LTMyLWJ5dGVzLWxvbmchIQ=="  # 32 bytes, base64


def test_matching_is_disabled_by_default():
    s = Settings()
    assert s.biometric_matching_enabled is False


def test_thresholds_and_model_dir_are_settings():
    """Calibrated before enabling, per plan decision 8 — so they must be
    env-tunable, not constants buried in the matcher."""
    s = Settings()
    assert 0 < s.face_match_threshold < 1
    assert 0 < s.face_match_margin < 1
    # A real, env-tunable path setting (the old truthiness assert could
    # never fail — any Path str is truthy).
    assert s.face_model_dir == Path("models/face")


def test_prod_cannot_enable_matching_without_a_notice_version():
    """The compliance artifact IS the data model: enrollment records which
    notice version the employee was shown, so production with matching on
    and no versioned notice must fail at construction, not at enrollment."""
    with pytest.raises(ValueError, match="biometric_notice_version"):
        Settings(
            env="prod",
            field_encryption_key=_REAL_KEY,
            biometric_matching_enabled=True,
        )


def test_prod_with_a_notice_version_may_enable_matching():
    s = Settings(
        env="prod",
        field_encryption_key=_REAL_KEY,
        biometric_matching_enabled=True,
        biometric_notice_version="2026-07-notice-v1",
    )
    assert s.biometric_matching_enabled is True


def test_dev_may_enable_matching_without_a_notice_version():
    s = Settings(biometric_matching_enabled=True)
    assert s.biometric_matching_enabled is True
    assert s.biometric_notice_version == ""


def test_prod_with_matching_off_needs_no_notice_version():
    s = Settings(env="prod", field_encryption_key=_REAL_KEY)
    assert s.biometric_matching_enabled is False
