"""Stop backfilled assignments from starting before the employee was hired.

`e1a0`'s backfill set `effective_from` to the payroll anchor (2026-01-05) for
EVERY employee, unconditionally. It guarded the other end -- `effective_to` uses
`GREATEST(termination_date + 1, ANCHOR)` so a pre-anchor termination cannot
invert the interval -- but never applied the symmetric guard to the start.

So anyone hired AFTER the anchor got an assignment backdated to it. Reproduced:
an employee with `hire_date` 2026-07-15 resolves as employed on 2026-02-01, five
and a half months before they were hired. That is not only a reporting oddity:

  - `employee_ids_with_primary_at` puts them in a PAY RUN for a period predating
    their employment, and
  - `employee_serves_property` returns True, so AUTHORIZATION admits them --
    a kiosk would let them punch in on a backdated business date.

This migration moves those starts forward to the hire date. It touches ONLY rows
the backfill created (`effective_from = ANCHOR`); assignments written by the real
onboarding path since E1 carry their own dates and are left alone.

`e1a0` itself is deliberately NOT edited. It has been applied, and rewriting an
applied migration means two databases at the same revision disagree about their
own history -- the exact failure this chain already has one instance of.
"""

from alembic import op

revision = "e1i0hiredate"
down_revision = "e1h0census"
branch_labels = None
depends_on = None

_ANCHOR = "2026-01-05"


def upgrade() -> None:
    # LEAST(..., effective_to) keeps the interval valid. An employee whose
    # hire_date falls after their own termination is contradictory data, and the
    # honest representation is a zero-width interval -- in force on no date --
    # rather than an inverted one the date predicates cannot reason about.
    op.execute(
        f"""
        UPDATE employee_assignment a
           SET effective_from = CASE
                 WHEN a.effective_to IS NULL
                   THEN GREATEST(e.hire_date, DATE '{_ANCHOR}')
                 ELSE LEAST(
                        GREATEST(e.hire_date, DATE '{_ANCHOR}'),
                        a.effective_to
                      )
               END
          FROM employee e
         WHERE e.employee_id = a.employee_id
           AND a.effective_from = DATE '{_ANCHOR}'
           AND e.hire_date IS NOT NULL
           AND e.hire_date > DATE '{_ANCHOR}'
        """
    )


def downgrade() -> None:
    # Deliberately NOT reverted. Moving these starts back to the anchor would
    # reinstate the bug -- employees resolving as employed, and admissible at a
    # kiosk, before they were hired. There is also no way to distinguish a row
    # this migration moved from one an operator set to the same date by hand, so
    # a faithful inverse does not exist. Downgrading leaves the corrected dates
    # in place, which is strictly safer than the state it would be restoring.
    pass
