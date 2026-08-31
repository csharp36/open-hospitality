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

    # "team", not "payroll" — LOAD-BEARING, do not swap back. `two_tenant_world`
    # builds on `founding_org`, which seeds org 1's payroll and accounting rows
    # unconditionally (D-OH17.15), and its own setup connects org 1's demand
    # feed too (`set_demand_feed(db_session, "delphi")`). All three integration
    # items already probe `done` for org 1 in this world, and D-B4.4 makes
    # `done` outrank a dismissal on purpose — so dismissing any of them and
    # asserting "dismissed" fails for a reason that has nothing to do with
    # tenancy (OH-17 Task 7 exposed this: the probes used to hardcode `False`,
    # which is why it passed before). "team" needs a SECOND distinct role
    # subject per org, and this world grants exactly one org_admin to org 1
    # and one to org 2, so it stays "open" for both — an item whose status has
    # no dependency on integration credentials, keeping this test about the L2
    # walls alone.
    with factory() as s:
        bind_org_context(s, 1)
        s.add(OrgChecklistOverride(org_id=1, item_key="team", created_by="a"))
        s.commit()

    with factory() as s:
        bind_org_context(s, 1)
        assert {r.key: r.status for r in evaluate(s)}["team"] == "dismissed"

    with factory() as s:
        bind_org_context(s, w.org2_id)
        assert {r.key: r.status for r in evaluate(s)}["team"] == "open"
