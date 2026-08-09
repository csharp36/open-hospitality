"""L8: close the tenancy gaps the three-lens review found in the L1 schema.

l1a0orgid is frozen for schema (its tuples are pinned literals), so the two
schema-level L8 findings land here, on the current head (l5a0orgsettings):

F2 — cross-org unique SQUATTING on the config FKs L1 deliberately kept
single-column. Two tables carry a GLOBAL one-per-parent unique whose
single-column FK a foreign org can reference (Postgres runs FK checks with
owner privileges, so RLS cannot stop the write): `pay_schedule` (one per
property) and — the re-audit twin — `labor_standard` (one per department).
An org-2 session plants a row naming org 1's property/department; org 1 is
then permanently denied its own, blocked by a row RLS makes invisible to it
— a cross-tenant denial of service. Promoting each to the composite
`(org_id, x_id)` FK onto the L1 parent unique makes the cross-org reference
structurally impossible, exactly as the L1 money/PII chain does. (The other
skipped config FKs back MULTI-column uniques — property_id + name/week/date —
where a squat blocks only a specific named slot the victim may never want,
not the whole capability; those stay single-column, the L1 judgment intact.)

F4 — the four `*_stage` uniques `(pms_source, business_date, row_hash)`
carry no org discriminator, and the stagers' `on_conflict_do_nothing` sits
on exactly that index, so a cross-org row_hash collision SILENTLY drops the
second org's row. Adding `org_id` to each unique (and to the stagers'
`index_elements`, in the same commit) keeps each org's row.

Downgrade restores the pre-L8 shape: the single-column FKs under their
ORIGINAL Postgres default names (older/deeper downgrades drop them by name)
and the org-free uniques under their original default names.
"""

from alembic import op
import sqlalchemy as sa

revision = "l8a0tenancyfix"
down_revision = "l5a0orgsettings"
branch_labels = None
depends_on = None

# (child table, single-col FK's ORIGINAL default name, local column, parent,
#  parent pk, composite name). The composite parent unique already exists from
# L1 (uq_property_org / uq_department_org).
_COMPOSITE = (
    ("pay_schedule", "pay_schedule_property_id_fkey", "property_id",
     "property", "property_id", "fk_pay_schedule_property_org"),
    ("labor_standard", "labor_standard_department_id_fkey", "department_id",
     "department", "department_id", "fk_labor_standard_department_org"),
)

# The four stage tables whose global (pms_source, business_date, row_hash)
# unique gains org_id. (table, new named unique).
_STAGE_UNIQUES = (
    ("pms_daily_financial_stage", "uq_pms_daily_financial_stage_org_row"),
    ("pms_daily_statistic_stage", "uq_pms_daily_statistic_stage_org_row"),
    ("pms_ledger_balance_stage", "uq_pms_ledger_balance_stage_org_row"),
    ("pms_daily_segment_stage", "uq_pms_daily_segment_stage_org_row"),
)

_STAGE_COLS = ("pms_source", "business_date", "row_hash")


def _unique_name(conn: sa.engine.Connection, table: str, columns: list[str]) -> str:
    """The name of the UNIQUE constraint on `table` covering exactly
    `columns` — looked up (not guessed) so a Postgres default name at the
    63-char truncation boundary cannot strand the migration."""
    return conn.execute(
        sa.text(
            "SELECT c.conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = :table AND n.nspname = 'public' "
            "AND c.contype = 'u' AND ("
            "  SELECT array_agg(a.attname ORDER BY a.attname) "
            "  FROM unnest(c.conkey) AS ck(attnum) "
            "  JOIN pg_attribute a ON a.attrelid = c.conrelid "
            "    AND a.attnum = ck.attnum"
            ") = :cols"
        ),
        {"table": table, "cols": sorted(columns)},
    ).scalar_one()


def upgrade() -> None:
    conn = op.get_bind()

    # F2: single-column config FK -> composite (org_id, x_id).
    for child, old_fk, col, parent, parent_pk, new_fk in _COMPOSITE:
        op.drop_constraint(old_fk, child, type_="foreignkey")
        op.create_foreign_key(
            new_fk, child, parent, ["org_id", col], ["org_id", parent_pk]
        )

    # F4: global stage unique -> org-scoped.
    for table, new_uq in _STAGE_UNIQUES:
        op.drop_constraint(
            _unique_name(conn, table, list(_STAGE_COLS)), table, type_="unique"
        )
        op.create_unique_constraint(new_uq, table, ["org_id", *_STAGE_COLS])


def downgrade() -> None:
    for table, new_uq in _STAGE_UNIQUES:
        op.drop_constraint(new_uq, table, type_="unique")
        # Recreate under the ORIGINAL Postgres default name so a deeper
        # rollback that drops it by that name still finds it.
        op.create_unique_constraint(
            f"{table}_pms_source_business_date_row_hash_key",
            table, list(_STAGE_COLS),
        )

    for child, old_fk, col, parent, parent_pk, new_fk in _COMPOSITE:
        op.drop_constraint(new_fk, child, type_="foreignkey")
        op.create_foreign_key(old_fk, child, parent, [col], [parent_pk])
