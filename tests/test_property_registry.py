"""`ensure_default_org` — the founding org and its env-seeded credentials.

OH-17 (D-OH17.15) turned the seed's one `org_settings` insert into a BRIDGE
that reconstructs org 1's integration credentials from the process-wide
`Settings`. The bridge is the only remaining place env becomes a credential;
everything downstream reads the row. These tests pin the three properties that
make that safe: it fires on the BARE defaults (the local e2e depends on it),
it respects the demand feed's OFF sentinel, and it never overwrites a row an
operator connected by hand.

The org-identity half of `ensure_default_org` (the explicit id, the sequence
floor, the alias) is pinned in `tests/test_founding_org_seed_rls.py` — that
world runs on the RLS-bound app role, which is what those invariants are
about. This file is the seed's DATA half on the owner session.
"""

from sqlalchemy import text

from usali.mapping.property_registry import ensure_default_org


def test_the_seed_writes_a_payroll_row_under_the_bare_defaults(db_session):
    """D-OH17.15. scripts/e2e_backend.py:399 relies on the Gusto defaults
    being a WORKING local config with no env set — a seed rule that only
    fired on non-default env would silently break payrun.spec.ts."""
    ensure_default_org(db_session)
    row = db_session.execute(text(
        "SELECT provider FROM org_integration_credential "
        "WHERE org_id = 1 AND integration = 'payroll'"
    )).scalar_one()
    assert row == "gusto"


def test_the_seed_writes_no_demand_feed_row_when_the_provider_is_unset(db_session):
    """'' is the OFF sentinel, and it stays off — demo.sh sets it explicitly."""
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
