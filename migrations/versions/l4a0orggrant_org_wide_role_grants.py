"""L4: org-wide role grants (Pillar L decision 4).

Role authority moves from realm token roles (realm-GLOBAL — an
org_admin of tenant A carries the role in a token used while active in
tenant B) to org-scoped `role_assignment` grants read under the active
org's RLS. The schema change is small and follows an existing
precedent exactly: `property_id` becomes nullable, NULL = ORG-WIDE
grant — the same shape as `department_id` NULL = property-wide.

Two judgments recorded here:

- **The unique constraint gains `NULLS NOT DISTINCT` (and `org_id`).**
  The old quad unique treated NULLs as distinct, so the same org-wide
  grant could be inserted twice, forever. A duplicate grant is not
  "harmless": grants are now THE role authority, and revocation is a
  row delete — with duplicates, deleting "the" grant row leaves a
  twin still granting, silently. Idempotent seeding also stops being
  verifiable ("did the re-seed duplicate?" has no constraint-backed
  answer). So: one grant, one row, enforced by the database —
  `UNIQUE NULLS NOT DISTINCT (org_id, keycloak_subject, role,
  property_id, department_id)`. `org_id` joins the key because the
  SAME subject may legitimately hold the SAME org-wide role in two
  orgs (two rows differing only in org_id), and because property ids
  are only unique per-org since L1. Pre-existing duplicate rows
  (possible under the old NULLs-distinct quad via NULL department_id)
  are deduplicated first, keeping the oldest row — the surviving row
  carries the identical grant, so no authority changes.

- **The L1 composite FK tolerates NULL property_id by construction.**
  `fk_role_assignment_property_org (org_id, property_id) ->
  property (org_id, property_id)` is MATCH SIMPLE (the Postgres
  default): a NULL in ANY referencing column skips enforcement
  entirely. That is exactly the wanted semantics — an org-wide grant
  names no property, so there is no property reference to check —
  and `org_id` itself stays enforced through the mixin's plain
  `fk_role_assignment_org -> organization` FK. Pinned in
  tests/test_l4_org_grants.py rather than re-declared here.

Downgrade restores NOT NULL, which requires DELETING org-wide rows
first: a NULL-property grant cannot exist in the pre-L4 schema at
all, so this is not the I6 carry-rows-through case (that lesson
covers rows the old schema can hold). Losing them on downgrade is
loud in the row count and honest — the pre-L4 world derives global
authority from token roles again, so the deleted rows would have
granted nothing there anyway.
"""

from alembic import op

revision = "l4a0orggrant"
down_revision = "l3a0aliaslookup"
branch_labels = None
depends_on = None

UNIQUE_NAME = "uq_role_assignment_grant"

# The pre-L4 unique's column list — used to (re)create it on downgrade
# with the same server-generated name it originally had, so any deeper
# rollback that addresses it by default name still finds it.
_OLD_UNIQUE_COLS = "keycloak_subject, role, property_id, department_id"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE role_assignment ALTER COLUMN property_id DROP NOT NULL"
    )
    # Dedupe grants that would collide under NULLS NOT DISTINCT: the old
    # quad unique treated NULL department_id rows as always-distinct, so
    # exact duplicates may exist. Keep the OLDEST row (min assignment_id)
    # of each grant — the survivor is the identical grant.
    op.execute(
        """
        DELETE FROM role_assignment a USING role_assignment b
        WHERE a.assignment_id > b.assignment_id
          AND a.org_id = b.org_id
          AND a.keycloak_subject = b.keycloak_subject
          AND a.role = b.role
          AND a.property_id IS NOT DISTINCT FROM b.property_id
          AND a.department_id IS NOT DISTINCT FROM b.department_id
        """
    )
    # The old unique's name was server-generated (and server-truncated);
    # drop it by lookup rather than transcribing the truncation here.
    op.execute(
        """
        DO $$
        DECLARE uq text;
        BEGIN
          SELECT conname INTO STRICT uq FROM pg_constraint
           WHERE conrelid = 'role_assignment'::regclass AND contype = 'u';
          EXECUTE format(
            'ALTER TABLE role_assignment DROP CONSTRAINT %I', uq);
        END $$
        """
    )
    op.execute(
        f"ALTER TABLE role_assignment ADD CONSTRAINT {UNIQUE_NAME} "
        "UNIQUE NULLS NOT DISTINCT "
        "(org_id, keycloak_subject, role, property_id, department_id)"
    )


def downgrade() -> None:
    # Org-wide grants cannot exist in the pre-L4 schema (NOT NULL
    # property_id) — see the module docstring for why deleting them is
    # the honest downgrade rather than an I6 violation.
    op.execute("DELETE FROM role_assignment WHERE property_id IS NULL")
    op.execute(
        f"ALTER TABLE role_assignment DROP CONSTRAINT {UNIQUE_NAME}"
    )
    # Recreated WITHOUT a name so Postgres regenerates the exact default
    # name the pre-L4 schema had.
    op.execute(
        f"ALTER TABLE role_assignment ADD UNIQUE ({_OLD_UNIQUE_COLS})"
    )
    op.execute(
        "ALTER TABLE role_assignment ALTER COLUMN property_id SET NOT NULL"
    )
