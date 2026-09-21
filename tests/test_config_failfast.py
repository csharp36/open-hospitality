import pytest

from usali.config import Settings

_DEV_DEFAULT = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEE="
_REAL_KEY = "bm90LWEtcmVhbC1rZXktYnV0LTMyLWJ5dGVzLWxvbmchIQ=="  # 32 bytes, base64

# A prod-env Settings must override EVERY dev-default secret in
# config._DEV_DEFAULT_SECRETS, not just the field key, or construction
# refuses on the first one still at its committed default.
_REAL_INTAKE = "not-the-committed-intake-secret"

_CREDENTIAL_URL_FIELDS = (
    "qbo_base_url",
    # The Intuit CONSENT host (OH-17): the operator's browser carries the
    # signed OAuth `state` there, so cleartext to a remote host hands out a
    # live cross-tenant credential-injection token.
    "qbo_authorize_url",
    # Its mirror, added 2026-08-31: the browser carries the state OUT to the
    # authorize host and the authorization CODE back to this one, which is
    # what `qbo_redirect_uri` is built from. Guarding only the outbound half
    # left the code's return trip unguarded.
    "public_base_url",
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
    s = Settings(env="prod", field_encryption_key=_REAL_KEY,
                 email_intake_secret=_REAL_INTAKE)
    assert s.env == "prod"


def test_dev_with_the_default_key_is_fine():
    s = Settings(env="dev")
    assert s.field_encryption_key == _DEV_DEFAULT


# --- The emailed-intake secret (OH-23, D-OH23.2) -----------------------------
# A second entry in config._DEV_DEFAULT_SECRETS: the value in the repo would let
# anyone with a checkout sign a mail-webhook call, so prod must refuse it the
# same way it refuses the dev field key.

_DEV_DEFAULT_INTAKE = "dev-intake-secret"


def test_prod_with_the_dev_default_intake_secret_is_refused():
    with pytest.raises(ValueError, match="email_intake_secret"):
        Settings(
            env="prod",
            field_encryption_key=_REAL_KEY,
            email_intake_secret=_DEV_DEFAULT_INTAKE,
        )


def test_prod_with_a_real_intake_secret_is_allowed():
    s = Settings(
        env="prod", field_encryption_key=_REAL_KEY, email_intake_secret=_REAL_INTAKE,
    )
    assert s.email_intake_secret == _REAL_INTAKE


def test_dev_with_the_default_intake_secret_is_fine():
    s = Settings(env="dev")
    assert s.email_intake_secret == _DEV_DEFAULT_INTAKE


@pytest.mark.parametrize("env", ["production", "PROD", "prod\n", "staging"])
def test_non_dev_envs_refuse_the_dev_default_intake_secret(env):
    with pytest.raises(ValueError, match="email_intake_secret"):
        Settings(env=env, field_encryption_key=_REAL_KEY)


def test_the_intake_window_and_cap_have_the_documented_defaults():
    s = Settings(env="dev")
    assert s.email_intake_window_seconds == 300
    assert s.email_intake_max_bytes == 25 * 1024 * 1024
    assert s.email_intake_domain == "intake.example.test"


# --- Fail closed: anything not explicitly dev/test/local is treated as prod ---
# The guard must NOT key off the exact string "prod": a typo like "production",
# a shouted "PROD", a trailing newline, or an arbitrary "staging" must ALL trip.


@pytest.mark.parametrize("env", ["production", "PROD", "prod\n", "staging"])
def test_non_dev_envs_refuse_the_dev_default_field_key(env):
    with pytest.raises(ValueError, match="field_encryption_key"):
        Settings(env=env, field_encryption_key=_DEV_DEFAULT)


@pytest.mark.parametrize("env", ["production", "PROD", "prod\n", "staging"])
def test_non_dev_envs_are_production(env):
    assert Settings(
        env=env, field_encryption_key=_REAL_KEY, email_intake_secret=_REAL_INTAKE,
    ).is_production is True


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
            email_intake_secret=_REAL_INTAKE,
            **{field_name: "http://api.example.com"},
        )


@pytest.mark.parametrize("field_name", _CREDENTIAL_URL_FIELDS)
def test_production_accepts_https_integration_urls(field_name):
    settings = Settings(
        env="prod",
        field_encryption_key=_REAL_KEY,
        email_intake_secret=_REAL_INTAKE,
        **{field_name: "https://api.example.com"},
    )
    assert getattr(settings, field_name) == "https://api.example.com"


@pytest.mark.parametrize("field_name", _CREDENTIAL_URL_FIELDS)
def test_non_production_allows_cleartext_remote_integration_urls(field_name):
    # The HTTPS requirement is gated on is_production. Outside production a
    # cleartext remote URL must pass untouched -- this is the direction that
    # kills the "drop the is_production gate" mutant (guard always on).
    settings = Settings(env="dev", **{field_name: "http://api.example.com"})
    assert getattr(settings, field_name) == "http://api.example.com"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_production_allows_cleartext_loopback_mocks(host):
    settings = Settings(
        env="prod",
        field_encryption_key=_REAL_KEY,
        email_intake_secret=_REAL_INTAKE,
        qbo_base_url=f"http://{host}:9200",
    )
    assert settings.qbo_base_url.startswith("http://")


def test_fail_closed_environment_alias_also_refuses_cleartext_remote_url():
    with pytest.raises(ValueError, match="adp_base_url"):
        Settings(
            env="staging",
            field_encryption_key=_REAL_KEY,
            email_intake_secret=_REAL_INTAKE,
            adp_base_url="http://payroll.example.com",
        )
