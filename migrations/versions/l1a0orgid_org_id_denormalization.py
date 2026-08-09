"""L1: `org_id` on every tenant-owned table — the schema wall groundwork.

Pillar L decision 1 (design/2026-08-01-d1-tenant-isolation-design.md):
one mechanical migration denormalizes the tenant key onto every
tenant-owned table so L2 can stand two independent walls on it (the
`do_orm_execute` criteria hook and per-table RLS). Per table: add the
column -> backfill to the single existing org (SELECTed, never assumed
-- A2.1 seeds exactly one, and this migration REFUSES a multi-org
database outright, since it predates the very concept) -> NOT NULL ->
FK to organization -> index.

## The tenant-owned judgment (decision 1: when in doubt, tenant-owned)

Org-scoped: all 44 remaining tables — every stage/fact/ledger row, the
whole workforce and wage world, scheduling, CRM demand, audit_event and
qbo_push_ledger (both are a tenant's operational history), and
`organization` itself (already keyed by org_id; a tenant reads only its
own row once L2's RLS lands). `property` carried org_id since A2.1 and
here gains only the mixin's index and server default.

Excluded — genuinely org-independent reference data, enumerated:

- `alembic_version`: migration bookkeeping, not data.
- `usali_schedule`: the USALI standard's schedule list, seeded from the
  repo's `mapping/usali_schedules.yaml`. A property of the STANDARD,
  not of any tenant.
- `usali_mapping_dictionary`: the curated PMS-code -> USALI mapping,
  seeded from the repo's per-PMS YAML and upserted on the org-free key
  (pms_source, pms_trx_code, usali_edition). It describes what an
  Opera/Autoclerk transaction code MEANS under the standard — platform
  curation shared by every tenant. Making it per-org would restructure
  that unique key, which is a product decision about divergent tenant
  mappings, not a mechanical denormalization; if that day comes it gets
  its own migration and its own review.

## The server default (org 1)

Every org_id column carries `server_default '1'` (the SELECTed org id
on a populated database — org 1 by construction). Deliberate, and NOT
the wage_jurisdiction case (e1f0nodefault): L1 ships schema only — no
session carries an org context until L2/L3 — so every existing write
path must keep landing rows in the single tenant unchanged. While
exactly one org exists the default cannot encode a wrong decision, and
the moment it could, L2's RLS WITH CHECK refuses any insert whose
org_id disagrees with the session org. The default is the bridge, RLS
is the wall.

## Composite FKs (the FK-bypass subtlety, judged per-table)

Postgres runs FK checks with OWNER privileges — RLS cannot stop a buggy
write from referencing another org's parent row it cannot even read.
Where that reference carries money or PII, the FK becomes composite
`(org_id, x_id)` onto a matching parent UNIQUE, so the schema itself
refuses the cross-org write. Composite here — the wage/PII spine:

- every FK onto `employee` (pay_run_line, pay_run_line_property,
  wage_settlement, timecard, punch, sick_leave_ledger,
  employee_assignment, shift, deposit_account, employee_payroll_profile,
  employee_face_template, provider_employee_ref, and the manager
  self-reference): the employee row is where paychecks, SSNs, bank
  chains and biometrics anchor; a cross-org reference either pays the
  wrong org's person or attaches PII across the wall.
- every FK onto `timecard` (punch, timecard_adjustment,
  sick_leave_ledger, usali_labor_fact): the timecard chain IS worked
  hours becoming money.
- every FK onto `pay_run` (pay_run_line, pay_run_line_property,
  wage_settlement, usali_actual_labor_fact): a cross-org line injects
  money into another org's run — or draws pay from it.
- assignment_rate -> employee_assignment: a rate prices a placement;
  priced onto another org's placement it is compensation crossing orgs.
- role_assignment -> property / department: an RBAC grant scoped to
  another org's hotel is authority across the wall (decision 4 makes
  these grants THE role authority).
- employee_assignment -> property / department / position: the
  employment record decides which property's run pays and what FLSA
  class prices the hours.
- timecard_adjustment -> property: an adjustment attributes corrected
  minutes (money) to a property P&L.
- punch -> kiosk_device: hour attribution flows through the device's
  property; a cross-org device prices hours onto a foreign P&L.
- department FKs on the money facts (pay_run_line,
  pay_run_line_property, usali_labor_fact, usali_actual_labor_fact):
  the department is the axis those dollars are attributed on.

Skipped — single-column FK kept, with reasoning (the plan's recorded-
skip requirement):

- ingestion lineage (every `ingest_batch_id`, and the stage_id FKs on
  usali_financial_fact / mapping_exception / usali_statistic_fact /
  usali_ledger_balance_fact): the parent holds file metadata; no money
  or PII rides the reference, and the child's own org_id fences reads
  once RLS lands. A cross-org lineage pointer is wrong provenance, not
  a payment or a disclosure.
- crm_demand_snapshot -> crm_pull_batch: decision-support demand data.
- property_detection_alias -> property: ingestion routing config; a
  cross-org alias misroutes an upload into the WRITER'S own org (facts
  land under the writer's org_id), disclosing and paying nothing.
- org-structure and scheduling config (department -> property,
  position -> department, kiosk_device -> property, pay_schedule ->
  property, crm_pull_batch -> property, shift_template / schedule /
  labor_standard / occupancy_forecast -> property/department, shift ->
  schedule/department/shift_template): configuration references; the
  rows carry no money and no PII, and under RLS a cross-org config
  parent is invisible to the victim org rather than disclosed. (A
  cross-org row could still SQUAT a unique slot, e.g. pay_schedule's
  one-per-property — a denial-of-service shape, not a leak; noted for
  the L8 tenancy lens.)

The dropped single-column FKs are recreated by downgrade under their
ORIGINAL names (three were explicitly named; the rest carry Postgres
default names) — older downgrades drop them BY NAME, so a renamed
restore would strand any deeper rollback.

Also lands `organization.kc_org_alias` (decision 3): the join key to
the Keycloak organization alias, resolved DB-side in L3 — nothing
numeric from a token is ever an org_id. String(100), NULLABLE (no org
has declared an alias yet; inventing one would bind the org to a
Keycloak object that does not exist), UNIQUE (two orgs answering to one
alias would make alias->org resolution ambiguous — the exact guessing
L3 refuses).

Downgrade drops constraints and columns and carries every row through
(the I6 lesson — exercised against populated tables in
test_migration_on_populated_data.py).

Operationally this is ONE transaction doing 42 x (ADD COLUMN + full-table
rewrite backfill + index build) plus 33 FK swaps — deliberate (a partial
denormalization is worse than a locked one), so on a production-sized
database expect table locks for the duration and run it in a maintenance
window.
"""

from alembic import op
import sqlalchemy as sa

revision = "l1a0orgid"
down_revision = "j3a0crmref"
branch_labels = None
depends_on = None

# Every tenant-owned table gaining the column. `property` (has it since
# A2.1) and `organization` (keyed by it) are handled separately.
_ORG_TABLES = (
    "ingest_batch",
    "pms_daily_financial_stage",
    "usali_financial_fact",
    "mapping_exception",
    "pms_daily_statistic_stage",
    "usali_statistic_fact",
    "pms_ledger_balance_stage",
    "usali_ledger_balance_fact",
    "qbo_push_ledger",
    "pms_daily_segment_stage",
    "usali_segment_fact",
    "property_detection_alias",
    "department",
    "position",
    "employee",
    "employee_assignment",
    "role_assignment",
    "audit_event",
    "kiosk_device",
    "punch",
    "employee_face_template",
    "timecard",
    "timecard_adjustment",
    "usali_labor_fact",
    "employee_payroll_profile",
    "deposit_account",
    "sick_leave_ledger",
    "pay_schedule",
    "provider_employee_ref",
    "pay_run",
    "pay_run_line",
    "assignment_rate",
    "pay_run_line_property",
    "wage_settlement",
    "crm_pull_batch",
    "crm_demand_snapshot",
    "usali_actual_labor_fact",
    "shift_template",
    "schedule",
    "shift",
    "labor_standard",
    "occupancy_forecast",
)

# The parents composite FKs land on: UNIQUE (org_id, pk). Redundant with
# the pk alone, and required — a composite FK needs a matching unique.
_PARENT_UNIQUES = (
    ("uq_employee_org", "employee", "employee_id"),
    ("uq_property_org", "property", "property_id"),
    ("uq_department_org", "department", "department_id"),
    ("uq_position_org", "position", "position_id"),
    ("uq_kiosk_device_org", "kiosk_device", "device_id"),
    ("uq_timecard_org", "timecard", "timecard_id"),
    ("uq_employee_assignment_org", "employee_assignment", "assignment_id"),
    ("uq_pay_run_org", "pay_run", "pay_run_id"),
)

# (old single-FK name, composite name, child, local column, parent,
# parent key). Old names: Postgres defaults except the three explicitly
# named in 4cc05c6c3763 / b7c1d4a90e21 / e1c0adjprop.
_COMPOSITE_FKS = (
    ("employee_manager_employee_id_fkey", "fk_employee_manager_org",
     "employee", "manager_employee_id", "employee", "employee_id"),
    ("employee_assignment_employee_id_fkey", "fk_employee_assignment_employee_org",
     "employee_assignment", "employee_id", "employee", "employee_id"),
    ("employee_assignment_property_id_fkey", "fk_employee_assignment_property_org",
     "employee_assignment", "property_id", "property", "property_id"),
    ("employee_assignment_department_id_fkey", "fk_employee_assignment_department_org",
     "employee_assignment", "department_id", "department", "department_id"),
    ("employee_assignment_position_id_fkey", "fk_employee_assignment_position_org",
     "employee_assignment", "position_id", "position", "position_id"),
    ("role_assignment_property_id_fkey", "fk_role_assignment_property_org",
     "role_assignment", "property_id", "property", "property_id"),
    ("role_assignment_department_id_fkey", "fk_role_assignment_department_org",
     "role_assignment", "department_id", "department", "department_id"),
    ("punch_employee_id_fkey", "fk_punch_employee_org",
     "punch", "employee_id", "employee", "employee_id"),
    ("punch_kiosk_device_id_fkey", "fk_punch_kiosk_device_org",
     "punch", "kiosk_device_id", "kiosk_device", "device_id"),
    ("fk_punch_timecard", "fk_punch_timecard_org",
     "punch", "timecard_id", "timecard", "timecard_id"),
    ("employee_face_template_employee_id_fkey", "fk_employee_face_template_employee_org",
     "employee_face_template", "employee_id", "employee", "employee_id"),
    ("timecard_employee_id_fkey", "fk_timecard_employee_org",
     "timecard", "employee_id", "employee", "employee_id"),
    ("timecard_adjustment_timecard_id_fkey", "fk_timecard_adjustment_timecard_org",
     "timecard_adjustment", "timecard_id", "timecard", "timecard_id"),
    ("fk_timecard_adjustment_property", "fk_timecard_adjustment_property_org",
     "timecard_adjustment", "property_id", "property", "property_id"),
    ("usali_labor_fact_department_id_fkey", "fk_usali_labor_fact_department_org",
     "usali_labor_fact", "department_id", "department", "department_id"),
    ("usali_labor_fact_timecard_id_fkey", "fk_usali_labor_fact_timecard_org",
     "usali_labor_fact", "timecard_id", "timecard", "timecard_id"),
    ("employee_payroll_profile_employee_id_fkey", "fk_employee_payroll_profile_employee_org",
     "employee_payroll_profile", "employee_id", "employee", "employee_id"),
    ("deposit_account_employee_id_fkey", "fk_deposit_account_employee_org",
     "deposit_account", "employee_id", "employee", "employee_id"),
    ("sick_leave_ledger_employee_id_fkey", "fk_sick_leave_ledger_employee_org",
     "sick_leave_ledger", "employee_id", "employee", "employee_id"),
    ("sick_leave_ledger_timecard_id_fkey", "fk_sick_leave_ledger_timecard_org",
     "sick_leave_ledger", "timecard_id", "timecard", "timecard_id"),
    ("provider_employee_ref_employee_id_fkey", "fk_provider_employee_ref_employee_org",
     "provider_employee_ref", "employee_id", "employee", "employee_id"),
    ("pay_run_line_pay_run_id_fkey", "fk_pay_run_line_pay_run_org",
     "pay_run_line", "pay_run_id", "pay_run", "pay_run_id"),
    ("pay_run_line_employee_id_fkey", "fk_pay_run_line_employee_org",
     "pay_run_line", "employee_id", "employee", "employee_id"),
    ("fk_pay_run_line_department", "fk_pay_run_line_department_org",
     "pay_run_line", "department_id", "department", "department_id"),
    ("assignment_rate_assignment_id_fkey", "fk_assignment_rate_assignment_org",
     "assignment_rate", "assignment_id", "employee_assignment", "assignment_id"),
    ("pay_run_line_property_pay_run_id_fkey", "fk_pay_run_line_property_pay_run_org",
     "pay_run_line_property", "pay_run_id", "pay_run", "pay_run_id"),
    ("pay_run_line_property_employee_id_fkey", "fk_pay_run_line_property_employee_org",
     "pay_run_line_property", "employee_id", "employee", "employee_id"),
    ("pay_run_line_property_department_id_fkey", "fk_pay_run_line_property_department_org",
     "pay_run_line_property", "department_id", "department", "department_id"),
    ("wage_settlement_pay_run_id_fkey", "fk_wage_settlement_pay_run_org",
     "wage_settlement", "pay_run_id", "pay_run", "pay_run_id"),
    ("wage_settlement_employee_id_fkey", "fk_wage_settlement_employee_org",
     "wage_settlement", "employee_id", "employee", "employee_id"),
    ("usali_actual_labor_fact_pay_run_id_fkey", "fk_usali_actual_labor_fact_pay_run_org",
     "usali_actual_labor_fact", "pay_run_id", "pay_run", "pay_run_id"),
    ("usali_actual_labor_fact_department_id_fkey", "fk_usali_actual_labor_fact_department_org",
     "usali_actual_labor_fact", "department_id", "department", "department_id"),
    ("shift_employee_id_fkey", "fk_shift_employee_org",
     "shift", "employee_id", "employee", "employee_id"),
)


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "organization",
        sa.Column("kc_org_alias", sa.String(length=100), nullable=True),
    )
    op.create_unique_constraint(
        "uq_organization_kc_org_alias", "organization", ["kc_org_alias"]
    )

    orgs = conn.execute(
        sa.text("SELECT org_id FROM organization ORDER BY org_id")
    ).scalars().all()
    if len(orgs) > 1:
        raise RuntimeError(
            "l1a0orgid predates multi-tenancy and can only backfill a "
            f"single-org database; found orgs {orgs} — it cannot decide "
            "which rows belong to whom, and guessing would move money"
        )
    if orgs and orgs != [1]:
        # "Org 1 by construction" is load-bearing, not decorative: the
        # OrgScoped mixin's server default IS the literal 1, so a lone org
        # with any other id would leave every future insert pointing at an
        # org that does not exist. Refuse rather than diverge.
        raise RuntimeError(
            f"the single org must be org 1 by construction (A2.1); found "
            f"{orgs} — the OrgScoped server default (1) would diverge from "
            "this database"
        )
    if not orgs:
        for table in _ORG_TABLES:
            count = conn.execute(
                sa.text(f"SELECT count(*) FROM {table}")
            ).scalar_one()
            if count:
                raise RuntimeError(
                    f"no organization row exists but {table} holds {count} "
                    "rows — nothing to backfill them to; seed the org first "
                    "(A2.1)"
                )
    # On an empty database nothing backfills; A2.1 seeds the founding org
    # as id 1, which is what the server default must point at.
    org = orgs[0] if orgs else 1

    for table in _ORG_TABLES:
        op.add_column(table, sa.Column("org_id", sa.Integer(), nullable=True))
        conn.execute(
            sa.text(f"UPDATE {table} SET org_id = :org"), {"org": org}
        )
        op.alter_column(
            table, "org_id",
            existing_type=sa.Integer(),
            nullable=False,
            server_default=sa.text(str(org)),
        )
        op.create_foreign_key(
            f"fk_{table}_org", table, "organization", ["org_id"], ["org_id"]
        )
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])

    # `property` has carried org_id since A2.1 — it gains only the
    # mixin's shape: the server default, the index, and the mixin's FK
    # name (the constraint itself is unchanged; renamed so the ORM and
    # the migrated schema agree on what it is called).
    op.alter_column(
        "property", "org_id",
        existing_type=sa.Integer(),
        server_default=sa.text(str(org)),
    )
    op.create_index("ix_property_org_id", "property", ["org_id"])
    op.execute(
        "ALTER TABLE property RENAME CONSTRAINT "
        "property_org_id_fkey TO fk_property_org"
    )

    for name, table, pk in _PARENT_UNIQUES:
        op.create_unique_constraint(name, table, ["org_id", pk])

    for old, new, child, col, parent, parent_pk in _COMPOSITE_FKS:
        op.drop_constraint(old, child, type_="foreignkey")
        op.create_foreign_key(
            new, child, parent, ["org_id", col], ["org_id", parent_pk]
        )


def downgrade() -> None:
    conn = op.get_bind()
    # The SYMMETRIC guard (L8/F1): upgrade() refuses a multi-org database
    # because it cannot decide which rows belong to whom; downgrade() must
    # refuse the SAME shape, because tearing down org_id here is the strictly
    # WORSE direction — it strips the tenant discriminator (and, above L1, the
    # RLS wall) from every table, commingling tenants with NO way to
    # re-attribute them, and the re-upgrade then refuses the multi-org shape,
    # stranding a live two-tenant database at the pre-tenancy schema. The whole
    # downgrade command runs in one transaction (env.py: no
    # transaction_per_migration), so this raise is ATOMIC: nothing above L1 is
    # torn down either. A single-org (or empty) database downgrades cleanly —
    # there is exactly one tenant, so dropping org_id loses no attribution.
    orgs = conn.execute(
        sa.text("SELECT org_id FROM organization ORDER BY org_id")
    ).scalars().all()
    if len(orgs) > 1:
        raise RuntimeError(
            "l1a0orgid cannot be downgraded on a multi-org database: found "
            f"orgs {orgs}. Dropping org_id would strip the tenant "
            "discriminator from every table and commingle these tenants with "
            "no way to re-attribute them — and the re-upgrade would then refuse "
            "the multi-org shape, stranding the database pre-tenancy. Refusing "
            "rather than destroying tenant attribution (the symmetric twin of "
            "the upgrade's single-org guard)."
        )

    for old, new, child, col, parent, parent_pk in reversed(_COMPOSITE_FKS):
        op.drop_constraint(new, child, type_="foreignkey")
        # The ORIGINAL name, exactly — older downgrades drop these by name.
        op.create_foreign_key(old, child, parent, [col], [parent_pk])

    for name, table, _pk in reversed(_PARENT_UNIQUES):
        op.drop_constraint(name, table, type_="unique")

    op.execute(
        "ALTER TABLE property RENAME CONSTRAINT "
        "fk_property_org TO property_org_id_fkey"
    )
    op.drop_index("ix_property_org_id", table_name="property")
    op.alter_column(
        "property", "org_id",
        existing_type=sa.Integer(),
        server_default=None,
    )

    for table in reversed(_ORG_TABLES):
        op.drop_index(f"ix_{table}_org_id", table_name=table)
        op.drop_constraint(f"fk_{table}_org", table, type_="foreignkey")
        op.drop_column(table, "org_id")

    op.drop_constraint(
        "uq_organization_kc_org_alias", "organization", type_="unique"
    )
    op.drop_column("organization", "kc_org_alias")
