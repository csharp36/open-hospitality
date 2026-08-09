"""L3: the alias-keyed lookup policy on `organization` (decision 3).

Org resolution must turn a Keycloak organization ALIAS (from the
token's validated membership claim) into an org_id BEFORE any org is
bound — but the l2a0rlswall policy on `organization` keys on
`app.org_id`, which is exactly what resolution is trying to learn: a
chicken-and-egg. The escape hatches that don't work here, considered
and rejected:

- SECURITY DEFINER helper owned by the table owner: FORCE ROW LEVEL
  SECURITY applies to the owner too (that is its point, see l2a0rlswall),
  so the definer body reads zero rows like everyone else.
- A role with BYPASSRLS to own such a helper: no serving-adjacent role
  may hold BYPASSRLS — the L2 posture this pillar exists to keep.
- Resolving via the owner/alembic engine at request time: the serving
  process must never hold owner credentials (K2/L2).

So resolution gets its own deliberately narrow door: a second
PERMISSIVE policy on `organization`, FOR SELECT only, keyed on a SECOND
transaction-local variable (`app.org_alias`, tenancy.RLS_ALIAS_VAR):

    kc_org_alias = NULLIF(current_setting('app.org_alias', true), '')

Why this cannot become a cross-org oracle or enumerator:

- Unset (or expired-to-'') variable → NULL comparison → the policy
  contributes zero rows, exactly like the org_id policy (the l2 NULLIF
  rationale applies verbatim).
- When set, it exposes AT MOST ONE row — kc_org_alias is UNIQUE
  (uq_organization_kc_org_alias, L1) — and only the row whose alias
  EQUALS the variable byte-for-byte: no pattern, no listing, no
  ordering probe. Learning "alias X exists" requires already knowing X.
- The application sets the variable in exactly one place
  (`usali.tenancy.resolve_org_id`) and only with an alias taken from
  the token's SIGNED membership claim after the X-Active-Org header was
  validated against it — a non-member alias is refused (403, naming
  nothing) before any SQL runs, so the variable never carries an
  attacker-chosen value that the token does not already vouch for.
- FOR SELECT only: the writing side of the wall (WITH CHECK) still has
  only the org_id policy — this door reads, it never writes.

Policies of the same (permissive) kind OR together, so a normally
org-bound transaction is unaffected: it sets `app.org_id`, never
`app.org_alias`, and sees exactly its own row as before.

Downgrade drops the policy — resolution then fails closed (every alias
lookup finds nothing under the app role), it does not fail open.
"""

from alembic import op

from usali.tenancy import RLS_ALIAS_VAR

revision = "l3a0aliaslookup"
down_revision = "l2a0rlswall"
branch_labels = None
depends_on = None

_POLICY = "org_alias_lookup"
_PREDICATE = (
    f"kc_org_alias = NULLIF(current_setting('{RLS_ALIAS_VAR}', true), '')"
)


def upgrade() -> None:
    op.execute(
        f"CREATE POLICY {_POLICY} ON organization FOR SELECT "
        f"USING ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY {_POLICY} ON organization")
