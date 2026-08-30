"""OH-17: per-tenant integration credentials (design D-OH17.1, D-OH17.5)."""

import pytest
from sqlalchemy import String, text
from sqlalchemy.exc import IntegrityError

from usali.crypto import EncryptedString
from usali.models import Base, OrgIntegrationCredential


def test_the_table_is_registered():
    assert "org_integration_credential" in Base.metadata.tables


def test_org_settings_is_gone():
    """D-OH17.1: crm_provider was OrgSettings' only column, so absorbing it
    into the credential row leaves an empty table — and an empty table is
    where the next drift grows back."""
    assert "org_settings" not in Base.metadata.tables
    assert not hasattr(__import__("usali.models", fromlist=["x"]), "OrgSettings")


def test_org_id_is_part_of_the_primary_key():
    """The OrgChecklistOverride shape: org-scoped by its own composite key,
    so both L2 walls confine it automatically."""
    pk = {c.name for c in OrgIntegrationCredential.__table__.primary_key}
    assert pk == {"org_id", "integration"}


def test_secrets_are_encrypted_and_identifiers_are_not():
    """ADR-005: only actual secrets pay the encryption tax. `refresh_token`,
    `api_token`, `client_secret`, `subscription_key`, and `api_key` are bearer
    material — anyone holding one can act as the tenant against the provider
    — so they are `EncryptedString`. `realm_id`, `company_id`, and `client_id`
    are identifiers, not secrets: they are useless without the matching
    secret, and reading one in plaintext during a support conversation is
    worth more than encrypting it. This split is deliberate — if you are
    "helpfully" encrypting a company id, or leaving a new secret column
    plaintext because it "doesn't look secret enough," this test is the place
    that disagrees with you."""
    table = OrgIntegrationCredential.__table__
    encrypted_columns = {
        "refresh_token",
        "api_token",
        "client_secret",
        "subscription_key",
        "api_key",
    }
    plaintext_columns = {"realm_id", "company_id", "client_id"}

    for name in encrypted_columns:
        assert isinstance(table.c[name].type, EncryptedString), name
    for name in plaintext_columns:
        assert isinstance(table.c[name].type, String), name
        assert not isinstance(table.c[name].type, EncryptedString), name


def test_the_check_refuses_a_gusto_row_carrying_an_api_key(db_session, founding_org):
    """D-OH17.5: the DB refuses a malformed credential row independently of
    the app import. The 'must be NULL' half is what stops a stale api_key
    surviving a switch from Tripleseat to Delphi."""
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, api_token, company_id, api_key, connected_by) "
            "VALUES (1, 'payroll', 'gusto', 'x', 'c1', 'leftover', 'sub')"
        ))
        db_session.flush()


def test_the_check_refuses_a_row_with_no_secret(db_session, founding_org):
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, connected_by) "
            "VALUES (1, 'demand_feed', 'delphi', 'sub')"
        ))
        db_session.flush()


def test_the_check_refuses_a_provider_from_another_integration(db_session, founding_org):
    with pytest.raises(IntegrityError):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, realm_id, refresh_token, connected_by) "
            "VALUES (1, 'demand_feed', 'qbo', 'r1', 'tok', 'sub')"
        ))
        db_session.flush()
