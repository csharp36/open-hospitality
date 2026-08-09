"""e1: timecard_adjustment.property_id

A punch carries its property implicitly via its kiosk device. An adjustment has
no device, so before this column a manager correction's minutes could not be
attributed to a property at all — and for the 21 of 28 pilot staff who work both
hotels, that means guessing which P&L carries the cost.

Deliberately NULLABLE rather than backfilled to a guess. Existing rows genuinely
do not record where the corrected work happened, and writing a plausible value
would make an estimate indistinguishable from a recorded fact. NULL is honest,
and usali.attribution allocates those minutes proportionally to the same day's
device-derived split.

Revision ID: e1c0adjprop
Revises: e1b0jurisdiction
"""

import sqlalchemy as sa
from alembic import op

revision = "e1c0adjprop"
down_revision = "e1b0jurisdiction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "timecard_adjustment",
        sa.Column("property_id", sa.String(length=50), nullable=True),
    )
    op.create_foreign_key(
        "fk_timecard_adjustment_property",
        "timecard_adjustment",
        "property",
        ["property_id"],
        ["property_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_timecard_adjustment_property", "timecard_adjustment", type_="foreignkey"
    )
    op.drop_column("timecard_adjustment", "property_id")
