"""J3: `property.crm_ref` — the CRM-side property identity.

The mapping change of plan decision 6: a property's CRM external ref (a
Delphi hotel ref, a Tripleseat location id) is declared in
`mapping/properties.yaml` and seeded onto `property`, the
wage_jurisdiction pattern verbatim. NULLABLE with NO default, on
purpose: NULL is the honest state for "no CRM speaks for this
property", and the pull path (J4) refuses it BY NAME — an unset ref
becomes a loud refusal naming the property, never a guessed pull.
Bounded at 100: a ref is an identifier, not a payload.

Existing rows come out NULL — nobody has declared a CRM identity for
them, and inventing one would point a pull at a hotel it does not own.

Downgrade drops the column, refs and all. That silences nothing: the
code that reads crm_ref is gone with the rollback, and the value is
re-seedable from the registry file at any time (it is declared there,
not authored in the DB).
"""

from alembic import op
import sqlalchemy as sa

revision = "j3a0crmref"
down_revision = "j2a0crmdemand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "property",
        sa.Column("crm_ref", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("property", "crm_ref")
