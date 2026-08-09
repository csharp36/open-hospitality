"""E5: drop the profile's bank columns. The deposit destination is a chain.

`e5a0deposit` copied these envelopes into `deposit_account` rows verbatim;
every consumer now reads the chain, and mypy enumerates zero readers of the
scalars. Leaving them would leave TWO places a deposit destination can live,
and the retiring one reads more naturally at a call site -- the e2b0 lesson:
any old branch nobody re-read would reach for the easy wrong one and route a
paycheck to a stale account, silently.

## Downgrade restores only what is honestly restorable

An employee whose ENTIRE chain is one still-`legacy_sealed` row gets their
envelopes copied back -- they are byte-identical to what these columns held,
still sealed under the legacy aad these columns imply. Anything else (a
multi-account chain, or any row re-sealed under a per-ordinal aad) has no
faithful representation in one undated scalar pair, and an envelope moved
here with the wrong aad would fail to open at send time anyway. Those
employees' columns stay EMPTY: their deposit data must be re-entered through
the old UI. Loudly wrong beats quietly wrong when the output is where
paychecks land.
"""

from alembic import op
import sqlalchemy as sa

revision = "e5b0dropbank"
down_revision = "e5a0deposit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("employee_payroll_profile", "bank_account_sealed")
    op.drop_column("employee_payroll_profile", "bank_routing_sealed")
    op.drop_column("employee_payroll_profile", "account_type")


def downgrade() -> None:
    op.add_column(
        "employee_payroll_profile",
        sa.Column("bank_account_sealed", sa.Text(), nullable=True),
    )
    op.add_column(
        "employee_payroll_profile",
        sa.Column("bank_routing_sealed", sa.Text(), nullable=True),
    )
    op.add_column(
        "employee_payroll_profile",
        sa.Column("account_type", sa.String(10), nullable=True),
    )
    # NULLIF undoes e5a0's '' never-sealed placeholder.
    op.execute(
        """
        UPDATE employee_payroll_profile p
           SET bank_account_sealed = NULLIF(d.sealed_account, ''),
               bank_routing_sealed = NULLIF(d.sealed_routing, ''),
               account_type = d.account_type
          FROM deposit_account d
         WHERE d.employee_id = p.employee_id
           AND d.legacy_sealed
           AND NOT EXISTS (
                 SELECT 1 FROM deposit_account o
                  WHERE o.employee_id = d.employee_id
                    AND o.deposit_account_id <> d.deposit_account_id
               )
        """
    )
