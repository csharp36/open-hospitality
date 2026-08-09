"""Drop `Employee.pay_rate`. Rates live on the PLACEMENT now.

The scalar this removes is the bug: one undated value per person, so a raise
entered today re-costed already-worked hours on the next re-promote. An April
period costing $160 became $240 because a raise dated 1 August was entered, and
a filed Schedule 14 stopped tying to what was filed. `assignment_rate` (e2a0)
replaced it with rates that belong to a placement and a date range, and E2 Task
4 cut every consumer over.

## Why the column goes rather than staying as a harmless duplicate

Leaving it would leave TWO places a rate can live, and the retiring one is the
one that reads more naturally at a call site. `Employee.pay_rate` on an ORM
object is an attribute access; the correct answer is a function call that needs
an assignment and a date. Any new code, or any old branch nobody re-read, would
reach for the easy wrong one -- and it would work, silently, at whatever rate
the scalar last held.

That is not hypothetical here: E1 Task 9 dropped three columns for the same
reason, and SQLAlchemy accepts a write to an attribute that maps to no column
without error, so two tests were quietly asserting nothing. Removing the column
is what turns "please use the resolver" into a type error.

## Irreversible by nature

`downgrade()` restores the column but CANNOT restore its values. One scalar
cannot hold a date range, so collapsing `assignment_rate` back would mean
picking a rate and a moment to call authoritative -- the exact guess E2 exists
to prevent. It restores the column EMPTY and says so, rather than fabricating
compensation data that would then be used to pay people.
"""

from alembic import op
import sqlalchemy as sa

revision = "e2b0droprate"
down_revision = "e2a0rates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("employee", "pay_rate")


def downgrade() -> None:
    # NULL for everyone. The rate history in `assignment_rate` cannot be
    # projected onto a single undated scalar without choosing which rate and
    # which date are the "real" ones. Anything paid off a restored column would
    # be a number this migration invented, so it invents nothing and leaves the
    # column empty -- loudly wrong beats quietly wrong when the output is wages.
    op.add_column(
        "employee", sa.Column("pay_rate", sa.String(), nullable=True)
    )
