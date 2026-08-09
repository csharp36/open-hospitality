import pytest

from usali.config import Settings

_DEV_DEFAULT = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEE="
_REAL_KEY = "bm90LWEtcmVhbC1rZXktYnV0LTMyLWJ5dGVzLWxvbmchIQ=="  # 32 bytes, base64


def test_prod_with_dev_default_field_key_is_refused():
    with pytest.raises(ValueError, match="field_encryption_key"):
        Settings(env="prod", field_encryption_key=_DEV_DEFAULT)


def test_prod_with_a_real_field_key_is_allowed():
    s = Settings(env="prod", field_encryption_key=_REAL_KEY)
    assert s.env == "prod"


def test_dev_with_the_default_key_is_fine():
    s = Settings(env="dev")
    assert s.field_encryption_key == _DEV_DEFAULT


# --- Fail closed: anything not explicitly dev/test/local is treated as prod ---
# The guard must NOT key off the exact string "prod": a typo like "production",
# a shouted "PROD", a trailing newline, or an arbitrary "staging" must ALL trip.


@pytest.mark.parametrize("env", ["production", "PROD", "prod\n", "staging"])
def test_non_dev_envs_refuse_the_dev_default_field_key(env):
    with pytest.raises(ValueError, match="field_encryption_key"):
        Settings(env=env, field_encryption_key=_DEV_DEFAULT)


@pytest.mark.parametrize("env", ["production", "PROD", "prod\n", "staging"])
def test_non_dev_envs_are_production(env):
    assert Settings(env=env, field_encryption_key=_REAL_KEY).is_production is True


@pytest.mark.parametrize("env", ["dev", "test", "local", " DEV ", "Local"])
def test_known_non_prod_envs_are_not_production_and_allow_dev_default(env):
    s = Settings(env=env)
    assert s.is_production is False
    assert s.field_encryption_key == _DEV_DEFAULT
