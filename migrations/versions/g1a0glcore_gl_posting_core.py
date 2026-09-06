"""OH-27 G1: the GL posting core tables (design D-OH27.1..7, D-OH27.12).

Five org-scoped tables — gl_account, journal_entry, journal_line,
gl_posting_ledger, gl_period_event — each ENABLE + FORCE RLS with the
org_wall policy, predicate byte-identical to l2a0rlswall's (pinned by
tests/test_l2_rls_wall.py::test_the_stacked_migrations_share_the_l2_rls_predicate).

Two firsts for this chain, both ADR-011 enforcement points:

- ck_journal_entry_balanced: a DEFERRABLE INITIALLY DEFERRED constraint
  trigger on journal_line — at commit, every touched entry's debits must
  equal its credits exactly. The builder checks first with a friendlier
  error; this trigger is the wall, and it firing at all is a bug report.
- REVOKE UPDATE, DELETE on journal_entry, journal_line, gl_period_event
  from the app role (the l2a0rlswall REVOKE idiom): append-only is a grant,
  not a convention. gl_posting_ledger keeps UPDATE — it points at the
  current entry and is MEANT to move.
"""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import APP_DB_ROLE, RLS_ORG_VAR

revision = "g1a0glcore"
down_revision = "b3a0integcred"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"

_TABLES = (
    "gl_account",
    "journal_entry",
    "journal_line",
    "gl_posting_ledger",
    "gl_period_event",
)

_IMMUTABLE = ("journal_entry", "journal_line", "gl_period_event")


def _wall(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def upgrade() -> None:
    op.create_table(
        "gl_account",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_gl_account_org"),
            primary_key=True,
        ),
        sa.Column("account_code", sa.String(length=20), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("account_type", sa.String(length=20), nullable=False),
        sa.Column("usali_schedule_id", sa.Integer(), nullable=True),
        sa.Column("usali_major_category", sa.String(length=100), nullable=True),
        sa.Column("usali_sub_category", sa.String(length=100), nullable=True),
        sa.Column("usali_line_item", sa.String(length=100), nullable=True),
        sa.Column("system_role", sa.String(length=40), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "account_type IN ('asset', 'contra_asset', 'liability', "
            "'equity', 'income', 'expense')",
            name="ck_gl_account_type",
        ),
    )
    op.create_index(
        "uq_gl_account_org_role",
        "gl_account",
        ["org_id", "system_role"],
        unique=True,
        postgresql_where=sa.text("system_role IS NOT NULL"),
    )

    op.create_table(
        "journal_entry",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_journal_entry_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("entry_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("memo", sa.String(length=255), nullable=True),
        sa.Column("reversal_of", sa.BigInteger(), nullable=True),
        sa.Column("posted_by", sa.String(length=64), nullable=False),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "entry_id", name="uq_journal_entry_org"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_journal_entry_property_org",
        ),
        # Composite on purpose (the shape every other org-crossing reference
        # here uses): a single-column FK on reversal_of would let the DB
        # accept a reversal pointing at another org's entry.
        sa.ForeignKeyConstraint(
            ["org_id", "reversal_of"],
            ["journal_entry.org_id", "journal_entry.entry_id"],
            name="fk_journal_entry_reversal_org",
        ),
    )
    op.create_index("ix_journal_entry_org_id", "journal_entry", ["org_id"])

    op.create_table(
        "journal_line",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_journal_line_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("line_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("account_code", sa.String(length=20), nullable=False),
        sa.Column("posting", sa.String(length=6), nullable=False),
        sa.Column("amount", sa.Numeric(15, 4), nullable=False),
        sa.Column("memo", sa.String(length=255), nullable=True),
        sa.Column(
            "fact_id",
            sa.BigInteger(),
            sa.ForeignKey("usali_financial_fact.fact_id", name="fk_journal_line_fact"),
            nullable=True,
        ),
        sa.CheckConstraint("posting IN ('Debit', 'Credit')", name="ck_journal_line_posting"),
        sa.CheckConstraint("amount > 0", name="ck_journal_line_amount_positive"),
        sa.ForeignKeyConstraint(
            ["org_id", "entry_id"],
            ["journal_entry.org_id", "journal_entry.entry_id"],
            name="fk_journal_line_entry_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "account_code"],
            ["gl_account.org_id", "gl_account.account_code"],
            name="fk_journal_line_account_org",
        ),
    )
    op.create_index("ix_journal_line_org_id", "journal_line", ["org_id"])

    op.create_table(
        "gl_posting_ledger",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_gl_posting_ledger_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "posting_ledger_id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("entry_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "property_id", "business_date", "source_type",
            name="uq_gl_posting_ledger_org_row",
        ),
        sa.CheckConstraint(
            "status IN ('posted', 'failed')", name="ck_gl_posting_ledger_status"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_gl_posting_ledger_property_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "entry_id"],
            ["journal_entry.org_id", "journal_entry.entry_id"],
            name="fk_gl_posting_ledger_entry_org",
        ),
    )
    op.create_index("ix_gl_posting_ledger_org_id", "gl_posting_ledger", ["org_id"])

    op.create_table(
        "gl_period_event",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_gl_period_event_org"),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "period_event_id", sa.BigInteger(), primary_key=True, autoincrement=True
        ),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("period_key", sa.String(length=10), nullable=False),
        sa.Column("event", sa.String(length=10), nullable=False),
        sa.Column("actor_subject", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("event IN ('close', 'reopen')", name="ck_gl_period_event_kind"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"],
            ["property.org_id", "property.property_id"],
            name="fk_gl_period_event_property_org",
        ),
    )
    op.create_index("ix_gl_period_event_org_id", "gl_period_event", ["org_id"])

    for table in _TABLES:
        _wall(table)

    # The balance wall (ADR-011 §4): checked at COMMIT so multi-line entries
    # can be inserted line by line inside one transaction.
    op.execute(
        """
        CREATE FUNCTION gl_entry_balance_check() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE imbalance numeric;
        BEGIN
            SELECT COALESCE(
                SUM(CASE WHEN posting = 'Credit' THEN amount ELSE -amount END), 0
            )
            INTO imbalance FROM journal_line WHERE entry_id = NEW.entry_id;
            IF imbalance <> 0 THEN
                RAISE EXCEPTION
                    'journal entry % is out of balance by %', NEW.entry_id, imbalance;
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ck_journal_entry_balanced
        AFTER INSERT ON journal_line
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION gl_entry_balance_check()
        """
    )

    # Append-only is a grant, not a convention (ADR-011 §2).
    for table in _IMMUTABLE:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {APP_DB_ROLE}")


def downgrade() -> None:
    for table in _IMMUTABLE:
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {APP_DB_ROLE}")
    op.execute("DROP TRIGGER ck_journal_entry_balanced ON journal_line")
    op.execute("DROP FUNCTION gl_entry_balance_check()")
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY {_POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.drop_table(table)
