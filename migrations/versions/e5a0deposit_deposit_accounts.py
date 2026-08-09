"""E5: multi-account sealed deposit accounts.

Creates `deposit_account` and moves the deposit destination off
`employee_payroll_profile` (which keeps identity PII: ssn, tax elections).

## The backfill copies ciphertext it cannot verify

Each profile holding any of the three retiring fields becomes ONE
`deposit_account` row: ordinal 1, allocation_type 'remainder' (a single
account receiving everything is exactly what the old schema meant),
`legacy_sealed = TRUE`. The envelopes copy VERBATIM -- the server cannot open
them outside the send path and MUST not open them here, so re-sealing under
the new per-ordinal aad is impossible by design. `legacy_sealed` tells
`sync_employees` to derive the OLD aad (`{employee_id}:bank_account` /
`{employee_id}:bank_routing`) for these rows. A profile with only one sealed
field present still backfills (incomplete -- preflight names it, exactly as
it named the missing field before E5). The aad is never stored anywhere: it
derives from row identity, which is what makes it a slot binding.

## Split from the drop on purpose

This migration only creates and backfills; `e5b0dropbank` removes the
retiring profile columns once every consumer reads the chain (the e2a0/e2b0
split -- the drop is what turns "please use the chain" into a type error,
and it happens only when mypy can enumerate zero readers). Until then the
legacy columns keep their original envelopes untouched, which is also what
makes THIS downgrade lossless: dropping the table loses nothing the old
schema can express.
"""

from alembic import op
import sqlalchemy as sa

revision = "e5a0deposit"
down_revision = "e3a0class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deposit_account",
        sa.Column("deposit_account_id", sa.Integer(), primary_key=True,
                  autoincrement=True),
        sa.Column("employee_id", sa.Integer(),
                  sa.ForeignKey("employee.employee_id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("allocation_type", sa.String(10), nullable=False),
        sa.Column("allocation_value", sa.Numeric(10, 2), nullable=True),
        sa.Column("account_type", sa.String(10), nullable=True),
        sa.Column("sealed_account", sa.Text(), nullable=False),
        sa.Column("sealed_routing", sa.Text(), nullable=False),
        sa.Column("legacy_sealed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("employee_id", "ordinal"),
        sa.CheckConstraint(
            "(allocation_type = 'remainder') = (allocation_value IS NULL)",
            name="ck_deposit_remainder_iff_valueless",
        ),
    )
    op.create_index(
        "uq_one_remainder_per_employee", "deposit_account", ["employee_id"],
        unique=True, postgresql_where=sa.text("allocation_type = 'remainder'"),
    )

    # sealed_account/sealed_routing are NOT NULL in the new table, but a legacy
    # profile may hold only one of the pair. '' is the honest placeholder for
    # "this slot was never sealed": it fails SealedEnvelope.parse loudly, and
    # preflight's presence check treats it as absent -- same named blocker the
    # missing column produced before E5. COALESCE keeps the copy verbatim
    # otherwise. account_type copies AS IS, including NULL: a profile that
    # never stated a type must not acquire one here -- 'checking' would be an
    # affirmative claim the employee never made, sent to the provider as the
    # ACH account class (the E5 review's finding). Unknown stays unknown and
    # preflight names it.
    op.execute(
        """
        INSERT INTO deposit_account
               (employee_id, ordinal, allocation_type, allocation_value,
                account_type, sealed_account, sealed_routing, legacy_sealed)
        SELECT employee_id, 1, 'remainder', NULL,
               account_type,
               COALESCE(bank_account_sealed, ''),
               COALESCE(bank_routing_sealed, ''),
               TRUE
          FROM employee_payroll_profile
         WHERE bank_account_sealed IS NOT NULL
            OR bank_routing_sealed IS NOT NULL
            OR account_type IS NOT NULL
        """
    )
    # The retiring profile columns are NOT dropped here. e5b0dropbank drops
    # them once every consumer reads the chain -- the e2a0/e2b0 split: create
    # and backfill first, drop only when mypy can enumerate zero readers.


def downgrade() -> None:
    # The legacy columns still exist at this revision and still hold their
    # original envelopes untouched (the backfill only COPIED), so dropping the
    # table loses nothing the old schema can express.
    op.drop_index("uq_one_remainder_per_employee", table_name="deposit_account")
    op.drop_table("deposit_account")
