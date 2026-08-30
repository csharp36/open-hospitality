"""One org's dismissal must be invisible to another — the L2 walls, applied
to the checklist. Uses the shared world and the exact idiom of
test_l7_two_org_walk.py:41."""

from usali.checklist import evaluate
from usali.db import make_session_factory
from usali.models import OrgChecklistOverride
from usali.tenancy import bind_org_context


def test_a_dismissal_does_not_leak_across_orgs(two_tenant_world, app_role_engine):
    w = two_tenant_world
    factory = make_session_factory(app_role_engine)

    with factory() as s:
        bind_org_context(s, 1)
        s.add(OrgChecklistOverride(org_id=1, item_key="payroll", created_by="a"))
        s.commit()

    with factory() as s:
        bind_org_context(s, 1)
        assert {r.key: r.status for r in evaluate(s)}["payroll"] == "dismissed"

    with factory() as s:
        bind_org_context(s, w.org2_id)
        assert {r.key: r.status for r in evaluate(s)}["payroll"] == "open"
