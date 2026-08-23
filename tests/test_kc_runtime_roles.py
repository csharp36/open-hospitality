"""Pins the realm-management roles the usali-admin service account is granted.

Issue #69: self-service signup completed every step except the last, because
`provision_tenant` drives the usali-admin client against Keycloak's admin API
and its service account held no realm-management roles at all -> HTTP 403 on
`GET /organizations`, and no tenant was ever created.

The role that authorises the ORGANIZATIONS endpoints is `manage-realm`, which
is not guessable from the name. It was measured on throwaway containers against
both the deployed 26.0.8 and 26.3.5:

    manage-realm alone .................... GET 200 / POST 201
    manage-users view-users query-users ... GET 403 / POST 403
    view-realm alone ...................... GET 403 / POST 403
    no roles .............................. GET 403 / POST 403

These tests exist because the obvious "fix" is wrong and looks right. There is
no `manage-organizations` role on realm-management in 26.0.8, creating a realm
with `organizationsEnabled=true` seeds zero org roles on 26.3.5 either (so a
version bump does not produce them), and hand-creating a same-named role STILL
403s -- Keycloak authorises those endpoints on its own seeded permissions, not
on a matching role name. Anyone reaching for `manage-organizations` here has
found a dead end that costs a deploy cycle to rediscover.

configure_auth.py is a deploy script, not part of the `usali` package, so it is
read and parsed rather than imported (the pattern in test_runtime_invariants).
"""

import ast
from pathlib import Path

import pytest

_SOURCE = Path("scripts/cloud/configure_auth.py")


def _literal(name: str) -> object:
    """Evaluate a module-level constant out of configure_auth.py's AST."""
    tree = ast.parse(_SOURCE.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level constant in {_SOURCE}")


def test_manage_realm_is_granted():
    # The whole point of the fix: without this role the org lookup 403s.
    assert "manage-realm" in _literal("_RUNTIME_RM_ROLES")


def test_manage_realm_is_required_not_merely_wanted():
    # A missing role must abort the auth pass loudly. Granting a partial set
    # "succeeds" and then fails at the first signup, which is the silent
    # failure this whole issue was.
    assert "manage-realm" in _literal("_RUNTIME_RM_REQUIRED")


def test_the_user_roles_provision_tenant_needs_are_granted():
    # Orgs are not the whole flow: provision_tenant also creates the
    # workspace's first admin user and assigns its roles.
    roles = set(_literal("_RUNTIME_RM_ROLES"))
    assert {"manage-users", "view-users", "query-users"} <= roles


@pytest.mark.parametrize("dead_end", ["manage-organizations", "view-organizations"])
def test_the_nonexistent_organization_roles_are_not_granted(dead_end):
    # These do not exist on realm-management in 26.0.8 and are not seeded by
    # 26.3.5 either. Requiring one aborts the auth pass on every run; merely
    # wanting one grants nothing and leaves signup 403ing. See the module
    # docstring for the measurements.
    #
    # Asserted against the constants, NOT the file text: the comment above
    # them names both roles on purpose, to warn the next person off.
    granted = set(_literal("_RUNTIME_RM_ROLES")) | set(_literal("_RUNTIME_RM_REQUIRED"))
    assert dead_end not in granted, (
        f"{dead_end} does not exist on realm-management; manage-realm is what "
        "authorises the organizations endpoints"
    )


def test_realm_admin_is_not_used():
    # realm-admin also works, and grants everything in the realm. manage-realm
    # is the narrower role that still authorises orgs, so prefer it.
    assert "realm-admin" not in set(_literal("_RUNTIME_RM_ROLES"))
