"""L4 test plumbing: plant role_assignment grant rows for minted tokens.

Since Pillar L decision 4, realm token roles gate only the coarse
operator/employee door — org authority is the org-scoped
`role_assignment` grants read under the active org's RLS. A world that
mints an operator token therefore ALSO plants the matching grant row
(the DB-side twin of the authkit default-org claim): one line per world,
mirroring exactly what the dev/demo seeds do for the personas.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import RoleAssignment

# authkit.mint_token's default subject.
DEFAULT_SUB = "user-1"


def grant_role(
    session: Session,
    role: str,
    *,
    sub: str = DEFAULT_SUB,
    property_id: str | None = None,
    department_id: int | None = None,
    org_id: int = 1,
) -> None:
    """Find-or-create ONE grant row and commit. `property_id=None` is an
    ORG-WIDE grant (the l4a0orggrant shape). Writes through the caller's
    (superuser, un-instrumented) session so worlds can plant grants for
    any org."""
    exists = session.execute(
        select(RoleAssignment).where(
            RoleAssignment.org_id == org_id,
            RoleAssignment.keycloak_subject == sub,
            RoleAssignment.role == role,
            RoleAssignment.property_id.is_not_distinct_from(property_id),
            RoleAssignment.department_id.is_not_distinct_from(department_id),
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(RoleAssignment(
            org_id=org_id, keycloak_subject=sub, role=role,
            property_id=property_id, department_id=department_id,
        ))
        session.commit()
