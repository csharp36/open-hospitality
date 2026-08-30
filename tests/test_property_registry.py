"""`ensure_default_org` — the founding org and its env-seeded credentials.

OH-17 (D-OH17.15) turned the seed's one `org_settings` insert into a BRIDGE
that reconstructs org 1's integration credentials from the process-wide
`Settings`. The bridge is the only remaining place env becomes a credential;
everything downstream reads the row. These tests pin the properties that make
that safe: it fires on the BARE defaults (the local e2e depends on it), it
writes a row for each integration that has no off state (payroll, accounting),
it respects the demand feed's OFF sentinel — the one that does — and it never
overwrites a row an operator connected by hand.

The org-identity half of `ensure_default_org` (the explicit id, the sequence
floor, the alias) is pinned in `tests/test_founding_org_seed_rls.py` — that
world runs on the RLS-bound app role, which is what those invariants are
about. This file is the seed's DATA half on the owner session.
"""

from sqlalchemy import text

from usali.config import get_settings
from usali.mapping.property_registry import ensure_default_org

# `get_settings()` is UNCACHED, so the seed reads whatever env the process
# happens to hold — and these tests must not. `scripts/demo.sh:91` exports
# USALI_CRM_PROVIDER=delphi and a developer running the suite from that shell
# would otherwise see the OFF-sentinel test fail for a reason that has nothing
# to do with the code. Each test pins the ONE variable it is about, the
# `crm_on` fixture's posture (tests/test_j4_crm_pull.py:107).


def test_the_seed_writes_a_payroll_row_under_the_bare_defaults(
    db_session, monkeypatch
):
    """D-OH17.15. scripts/e2e_backend.py:399 relies on the Gusto defaults
    being a WORKING local config with no env set — a seed rule that only
    fired on non-default env would silently break payrun.spec.ts.

    `company_id` is asserted as well as the provider name, because a mutant
    that seeded a constant instead of reading `Settings` would satisfy the
    provider check alone. It is the identifier half of the pair on purpose:
    identifiers are PLAINTEXT (ADR-005), so this raw-SQL read returns the
    value. The same read against `api_token` would return ciphertext — that
    column is `EncryptedString`, decrypted only on the ORM path."""
    monkeypatch.setenv("USALI_PAYROLL_PROVIDER", "gusto")
    ensure_default_org(db_session)
    provider, company_id = db_session.execute(text(
        "SELECT provider, company_id FROM org_integration_credential "
        "WHERE org_id = 1 AND integration = 'payroll'"
    )).one()
    assert provider == "gusto"
    assert company_id == get_settings().gusto_company_id


def test_the_seed_writes_an_accounting_row(db_session, monkeypatch):
    """Accounting has NO off state — there is no `USALI_ACCOUNTING_PROVIDER`
    and qbo is the whole accounting half of the closed provider set — so the
    row is unconditional. Pinned because deleting the accounting seed entirely
    is otherwise invisible here, and `checklist._probe_accounting` will read
    this row for its answer."""
    monkeypatch.delenv("USALI_CRM_PROVIDER", raising=False)
    ensure_default_org(db_session)
    assert db_session.execute(text(
        "SELECT provider, realm_id FROM org_integration_credential "
        "WHERE org_id = 1 AND integration = 'accounting'"
    )).one() == ("qbo", get_settings().qbo_realm_id)


def test_the_seed_writes_no_demand_feed_row_when_the_provider_is_unset(
    db_session, monkeypatch
):
    """'' is the OFF sentinel, and it stays off — demo.sh sets it explicitly."""
    monkeypatch.delenv("USALI_CRM_PROVIDER", raising=False)
    ensure_default_org(db_session)
    assert db_session.execute(text(
        "SELECT count(*) FROM org_integration_credential "
        "WHERE integration = 'demand_feed'"
    )).scalar_one() == 0


def test_a_reseed_does_not_overwrite_an_operator_set_row(db_session):
    """The crm_ref find-or-create posture: a bare re-seed must never blank a
    credential an operator connected by hand."""
    ensure_default_org(db_session)
    db_session.execute(text(
        "UPDATE org_integration_credential SET company_id = 'operator-chosen' "
        "WHERE integration = 'payroll'"
    ))
    db_session.commit()
    ensure_default_org(db_session)
    assert db_session.execute(text(
        "SELECT company_id FROM org_integration_credential "
        "WHERE integration = 'payroll'"
    )).scalar_one() == "operator-chosen"
