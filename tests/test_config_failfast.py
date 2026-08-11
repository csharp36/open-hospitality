import pytest

from usali.config import Settings

_DEV_DEFAULT = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEE="
_REAL_KEY = "bm90LWEtcmVhbC1rZXktYnV0LTMyLWJ5dGVzLWxvbmchIQ=="  # 32 bytes, base64

_CREDENTIAL_URL_FIELDS = (
    "qbo_base_url",
    "gusto_base_url",
    "adp_base_url",
    "delphi_base_url",
    "tripleseat_base_url",
    "kc_admin_base_url",
)


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


@pytest.mark.parametrize("field_name", _CREDENTIAL_URL_FIELDS)
def test_production_refuses_cleartext_remote_integration_urls(field_name):
    with pytest.raises(ValueError, match=field_name):
        Settings(
            env="prod",
            field_encryption_key=_REAL_KEY,
            **{field_name: "http://api.example.com"},
        )


@pytest.mark.parametrize("field_name", _CREDENTIAL_URL_FIELDS)
def test_production_accepts_https_integration_urls(field_name):
    settings = Settings(
        env="prod",
        field_encryption_key=_REAL_KEY,
        **{field_name: "https://api.example.com"},
    )
    assert getattr(settings, field_name) == "https://api.example.com"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_production_allows_cleartext_loopback_mocks(host):
    settings = Settings(
        env="prod",
        field_encryption_key=_REAL_KEY,
        qbo_base_url=f"http://{host}:9200",
    )
    assert settings.qbo_base_url.startswith("http://")


def test_fail_closed_environment_alias_also_refuses_cleartext_remote_url():
    with pytest.raises(ValueError, match="adp_base_url"):
        Settings(
            env="staging",
            field_encryption_key=_REAL_KEY,
            adp_base_url="http://payroll.example.com",
        )
