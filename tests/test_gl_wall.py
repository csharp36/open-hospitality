"""The GL's three database walls (ADR-011 §2, §4; design D-OH27.4, .12).

Isolation: the org_wall policy on the GL tables, probed through the app
role with raw SQL so the DATABASE wall alone answers (the `text()` idiom
from test_integrations' two-org section — the ORM hook is nowhere in the
statement). Immutability: UPDATE/DELETE on the journal is refused BY
GRANT for the app role; g1a0glcore's REVOKE is the enforcement point.
Balance: an unbalanced entry is refused at COMMIT by the deferred
constraint trigger ck_journal_entry_balanced — including when the org
GUC is cleared or switched between insert and COMMIT, where the
trigger's visibility sentinel must fail loud rather than sum a hidden
set and call it balanced.

Everything here runs on `app_role_engine` (the RLS-bound, non-owner
`usali_app` role), never the superuser engine: a superuser bypasses RLS
no matter the policy, and holds privileges no REVOKE touched.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from usali.db import make_session_factory
from usali.models import GlAccount, JournalEntry, JournalLine, Property
from usali.tenancy import FOUNDING_ORG_ID, RLS_ORG_VAR, OrgBoundSessionFactory


def _app_factory_for(app_role_engine, org_id):
    """An org-bound session factory over the APP-ROLE engine — the
    test_l2_rls_wall / test_integrations wrapper shape."""
    return OrgBoundSessionFactory(make_session_factory(app_role_engine), org_id)


def _seed_minimal_books(factory, property_id):
    """One property, two accounts, one balanced 100.00 entry for whichever
    org `factory` is bound to; returns the entry_id.

    Everything is added WITHOUT org_id: the write wall stamps it from the
    session's bound context (the `_connect_bound` shape from
    test_integrations), which is also why one helper serves both orgs.
    The property is created here because `founding_org` seeds none, and
    `journal_entry` carries a composite FK onto (org_id, property_id).
    """
    with factory() as s:
        s.add(Property(property_id=property_id, name=f"GL Wall {property_id}",
                       pms_source="opera"))
        s.flush()
        # T-prefixed codes: the Task 4 chart template carries 4000/1210, and
        # provision_tenant seeds it — bare codes would collide on the
        # (org_id, account_code) PK once that lands. No system_role either:
        # the seeded "1210" already claims "guest_ledger_clearing" for this
        # org, and uq_gl_account_org_role is unique per (org_id, system_role)
        # — nothing below reads this account's role, only its code.
        s.add(GlAccount(account_code="T4000", name="Room Revenue",
                        account_type="income", is_active=True))
        s.add(GlAccount(account_code="T1210", name="Guest Ledger Clearing",
                        account_type="asset", is_active=True))
        s.flush()
        entry = JournalEntry(property_id=property_id,
                             business_date=date(2026, 7, 7),
                             source_type="test", source_hash="x" * 64,
                             posted_by="test")
        s.add(entry)
        s.flush()
        s.add(JournalLine(entry_id=entry.entry_id, account_code="T4000",
                          posting="Credit", amount=Decimal("100.0000")))
        s.add(JournalLine(entry_id=entry.entry_id, account_code="T1210",
                          posting="Debit", amount=Decimal("100.0000")))
        entry_id = entry.entry_id
        s.commit()
        return entry_id


def _counts(session):
    """Row counts per GL table through raw SQL — what the DATABASE wall
    alone permits this session to see."""
    return {
        table: session.execute(
            text(f"SELECT count(*) FROM {table}")  # noqa: S608
        ).scalar_one()
        for table in ("gl_account", "journal_entry", "journal_line")
    }


# ------------------------------------------------------------- isolation


def test_one_org_cannot_read_anothers_journal(app_role_engine, two_tenant_world):
    """Both directions, with positive controls: each org seeds its own
    books and must see exactly its own — a session that saw NOTHING (GUC
    unset, empty tables) would fail the controls, not slip through."""
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    org_b = _app_factory_for(app_role_engine, two_tenant_world.org2_id)
    _seed_minimal_books(org_a, "GLA1")
    _seed_minimal_books(org_b, "GLB2")

    tables = ("gl_account", "journal_entry", "journal_line")
    for factory, own_org in ((org_a, FOUNDING_ORG_ID),
                             (org_b, two_tenant_world.org2_id)):
        with factory() as s:
            # Positive control: this org's OWN books are all visible. The
            # gl_account control names the two seeded codes rather than an
            # exact total: once Task 4 wires the chart template into
            # provisioning, an org's account count includes the template.
            counts = _counts(s)
            assert counts["journal_entry"] == 1
            assert counts["journal_line"] == 2
            seeded = s.execute(
                text("SELECT count(*) FROM gl_account "
                     "WHERE account_code IN ('T4000', 'T1210')"),
            ).scalar_one()
            assert seeded == 2
            # And nothing from any other org is, in any GL table.
            for table in tables:
                n = s.execute(
                    text(f"SELECT count(*) FROM {table} "  # noqa: S608
                         "WHERE org_id <> :org"),
                    {"org": own_org},
                ).scalar_one()
                assert n == 0, f"org {own_org} can see another org's {table} rows"


# ----------------------------------------------------------- immutability


def test_the_journal_refuses_update_and_delete_by_grant(
    app_role_engine, founding_org
):
    """Append-only is a grant, not a convention: as the app role, UPDATE
    and DELETE on the journal die on privilege — inside the org's own
    context, on the org's own rows, so RLS is not what refuses them."""
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    entry_id = _seed_minimal_books(org_a, "GLA1")

    with org_a() as s:
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(
                text("UPDATE journal_entry SET memo = 'tampered' "
                     "WHERE entry_id = :e"),
                {"e": entry_id},
            )
    with org_a() as s:
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(
                text("DELETE FROM journal_line WHERE entry_id = :e"),
                {"e": entry_id},
            )
    with org_a() as s:
        # Positive control: the entry and both lines are still intact.
        n = s.execute(
            text("SELECT count(*) FROM journal_line WHERE entry_id = :e"),
            {"e": entry_id},
        ).scalar_one()
        assert n == 2


def test_the_append_only_revoke_covers_the_whole_journal(app_role_engine):
    """The behavioral test above proves the refusal is LIVE on two
    statement shapes; this one proves the SET is complete — every
    (immutable table, privilege) pair g1a0glcore revokes stays revoked,
    so a later migration re-granting one fails here, not in an audit.
    `has_table_privilege` evaluates as the connecting role: usali_app."""
    with app_role_engine.connect() as conn:
        for table in ("journal_entry", "journal_line", "gl_period_event"):
            for priv in ("UPDATE", "DELETE"):
                assert not conn.execute(
                    text("SELECT has_table_privilege(:t, :p)"),
                    {"t": table, "p": priv},
                ).scalar_one(), f"{priv} on {table} was re-granted"


# ---------------------------------------------------------------- balance


def test_an_unbalanced_entry_is_refused_at_commit(app_role_engine, founding_org):
    """A credit with no balancing debit flushes fine (the trigger is
    deferred so multi-line entries can insert line by line) and is
    refused at COMMIT."""
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    _seed_minimal_books(org_a, "GLA1")

    with org_a() as s:
        entry = JournalEntry(property_id="GLA1",
                             business_date=date(2026, 7, 8),
                             source_type="test", source_hash="y" * 64,
                             posted_by="test")
        s.add(entry)
        s.flush()
        s.add(JournalLine(entry_id=entry.entry_id, account_code="T4000",
                          posting="Credit", amount=Decimal("100.0000")))
        # No balancing debit: the deferred trigger must refuse at COMMIT.
        with pytest.raises(IntegrityError, match="out of balance"):
            s.commit()

    with org_a() as s:
        # The refused entry left nothing behind; the seed entry stands alone.
        assert _counts(s)["journal_entry"] == 1


def _insert_unbalanced_entry_raw(conn, org_id, property_id):
    """On an open app-role connection whose GUC already names `org_id`:
    insert an entry with a single unbalanced credit line, raw SQL.

    UNBALANCED on purpose, in every caller: if the trigger's visibility
    sentinel were removed, its SUM over the RLS-hidden line set would be
    0 — 'balanced' — and the commit would succeed, failing the test.
    """
    entry_id = conn.execute(
        text("INSERT INTO journal_entry (org_id, property_id, business_date,"
             " source_type, source_hash, posted_by)"
             " VALUES (:org, :p, :d, 'test', :h, 'test') RETURNING entry_id"),
        {"org": org_id, "p": property_id, "d": date(2026, 7, 9), "h": "z" * 64},
    ).scalar_one()
    conn.execute(
        text("INSERT INTO journal_line (org_id, entry_id, account_code,"
             " posting, amount) VALUES (:org, :e, 'T4000', 'Credit', 100.0000)"),
        {"org": org_id, "e": entry_id},
    )


def test_the_balance_trigger_fails_loud_when_rls_blinds_it(
    app_role_engine, founding_org
):
    """The visibility sentinel (g1a0glcore): clear the org GUC between
    the insert and COMMIT, and the deferred trigger — which then cannot
    see the very line that queued it — must refuse the commit rather
    than sum a hidden set and call the entry balanced. This test is the
    pin that makes removing the sentinel a failure."""
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    _seed_minimal_books(org_a, "GLA1")

    # A raw connection, not the ORM factory: the point is to control the
    # GUC mid-transaction, which OrgBoundSessionFactory exists to prevent.
    with app_role_engine.connect() as conn:
        conn.execute(text("SELECT set_config(:var, :org, true)"),
                     {"var": RLS_ORG_VAR, "org": str(FOUNDING_ORG_ID)})
        _insert_unbalanced_entry_raw(conn, FOUNDING_ORG_ID, "GLA1")
        conn.execute(text("SELECT set_config(:var, '', true)"),
                     {"var": RLS_ORG_VAR})
        with pytest.raises(IntegrityError, match="cannot see journal_line"):
            conn.commit()

    with org_a() as s:
        # Nothing landed: the refused transaction rolled back whole.
        assert _counts(s)["journal_entry"] == 1


def test_the_balance_trigger_fails_loud_when_the_guc_switches_orgs(
    app_role_engine, two_tenant_world
):
    """The other blinding: the GUC SWITCHES to a different real org
    before COMMIT. The org-1 line is just as hidden from the trigger as
    with no GUC at all, and the refusal must be the same."""
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    _seed_minimal_books(org_a, "GLA1")

    with app_role_engine.connect() as conn:
        conn.execute(text("SELECT set_config(:var, :org, true)"),
                     {"var": RLS_ORG_VAR, "org": str(FOUNDING_ORG_ID)})
        _insert_unbalanced_entry_raw(conn, FOUNDING_ORG_ID, "GLA1")
        conn.execute(text("SELECT set_config(:var, :org, true)"),
                     {"var": RLS_ORG_VAR, "org": str(two_tenant_world.org2_id)})
        with pytest.raises(IntegrityError, match="cannot see journal_line"):
            conn.commit()

    with org_a() as s:
        assert _counts(s)["journal_entry"] == 1
