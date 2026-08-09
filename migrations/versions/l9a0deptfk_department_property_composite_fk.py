"""L9: promote department.property_id to the composite (org_id, property_id) FK.

REVISES AN L8 JUDGMENT, deliberately and on the record. l8a0tenancyfix
promoted `pay_schedule` and `labor_standard` to composite FKs and explicitly
DECLINED to promote the rest, `department` among them, reasoning:

    "The other skipped config FKs back MULTI-column uniques — property_id +
    name/week/date — where a squat blocks only a specific named slot the
    victim may never want, not the whole capability."

That reasoning is sound for the threat it was aimed at — cross-org UNIQUE
squatting as denial of service — and it still holds for that threat. It is not
the whole question anymore. L8 was written when a department was born inside
the roster seed, from a property the SERVER had already resolved. Since then
`POST /api/departments` ships, and it takes `property_id` from the caller's
request body.

That changes what the single-column FK does. Postgres validates a foreign key
with the referenced table's OWNER privileges, so the check runs past RLS: an
org-1 session naming an org-2 property finds the FK satisfied and the row
lands, a department in org 1 anchored to a hotel org 1 cannot see. Not a
denial of service — a cross-tenant WRITE, which is the failure mode L1's
composite chain exists to make unrepresentable.

The endpoint now checks the property's visibility itself (workforce
`_require_onboardable_property`), and that check is the door. This is the wall
behind it: a scope check is code someone can forget to call, a composite FK is
the database refusing. Pillar L's whole posture is that money and tenancy
seams get both.

`department` already carries `org_id` (L1) and `property` already carries the
`uq_property_org` target (L1), so this is constraint surgery with no data
rewrite. If any existing department disagrees with its property's org, the
ADD CONSTRAINT fails loudly here rather than being silently accepted — which
is the correct outcome: such a row is precisely what this constraint is for.

Downgrade restores the single-column FK under its ORIGINAL Postgres default
name, so an older/deeper downgrade that drops it by that name still finds it.
"""

from alembic import op

revision = "l9a0deptfk"
down_revision = "l8a0tenancyfix"
branch_labels = None
depends_on = None

_OLD_FK = "department_property_id_fkey"
_NEW_FK = "fk_department_property_org"


def upgrade() -> None:
    op.drop_constraint(_OLD_FK, "department", type_="foreignkey")
    op.create_foreign_key(
        _NEW_FK, "department", "property",
        ["org_id", "property_id"], ["org_id", "property_id"],
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_FK, "department", type_="foreignkey")
    op.create_foreign_key(
        _OLD_FK, "department", "property", ["property_id"], ["property_id"]
    )
