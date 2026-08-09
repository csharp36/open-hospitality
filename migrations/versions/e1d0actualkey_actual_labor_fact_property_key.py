"""e1: widen usali_actual_labor_fact unique key to include property_id

Since E1 one pay run writes actual labor facts for EVERY property the employee
worked at — the paycheck is issued by their primary property, but the cost
belongs to each. The department snapshot on a pay line is the PRIMARY
assignment's department, so both properties' rows carried the same
department_id and the second insert violated the old
(pay_run_id, department_id) key. fetch_pay_run_results raised for every
employee working two properties, which is the headline case E1 exists for.

ORDERING NOTE: this migration must not land before reporting._labor_variance is
property-scoped. Widening the key alone converts a hard crash into a SILENT
cross-property disclosure — the other property's money appearing on this
property's statement under this property's headcount. Both ship together.

Revision ID: e1d0actualkey
Revises: e1c0adjprop
"""

from alembic import op

revision = "e1d0actualkey"
down_revision = "e1c0adjprop"
branch_labels = None
depends_on = None

_OLD = "usali_actual_labor_fact_pay_run_id_department_id_key"
_NEW = "uq_actual_labor_fact_run_property_department"


def upgrade() -> None:
    op.drop_constraint(_OLD, "usali_actual_labor_fact", type_="unique")
    op.create_unique_constraint(
        _NEW, "usali_actual_labor_fact", ["pay_run_id", "property_id", "department_id"]
    )


def downgrade() -> None:
    # Only reversible while no run has written facts for more than one property;
    # otherwise the narrower key cannot be recreated.
    op.drop_constraint(_NEW, "usali_actual_labor_fact", type_="unique")
    op.create_unique_constraint(
        _OLD, "usali_actual_labor_fact", ["pay_run_id", "department_id"]
    )
