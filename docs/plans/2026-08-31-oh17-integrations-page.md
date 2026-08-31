# `/integrations` Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `/integrations` SPA page so a hotel can connect, re-connect and disconnect its own payroll, accounting and demand-feed accounts from inside the app — closing the two checklist links that currently point at a route that does not exist.

**Architecture:** The API grows a `providers` block on its existing `GET /api/integrations`, derived from `PROVIDERS`, so the frontend holds no copy of the credential field lists. The QBO callback's failure paths become redirects rather than raises, in two shapes, so the anti-oracle property across bad states survives. The page is one container (`IntegrationsPage.tsx`) owning all fetching, with presentational children.

**Tech Stack:** FastAPI + pydantic + SQLAlchemy (backend, pytest); React + TanStack Router + TanStack Query + Tailwind (frontend, vitest + Testing Library).

**Design doc:** `docs/design/2026-08-31-oh17-integrations-page-design.md`

**Branch:** `feat/oh17-integrations-page` (already created)

---

## File Structure

**Backend — modified**

- `src/usali/integrations.py` — `ProviderSpec` gains `oauth`; a new public `product_name()` accessor; `_PRODUCT_NAMES`'s comment corrected.
- `src/usali/integrations_api.py` — three new pydantic models, `providers` on `IntegrationModel`, and the callback's failure paths turned into redirects.

**Backend — tests modified**

- `tests/test_integrations_api.py` — provider-spec exposure.
- `tests/test_integrations_oauth.py` — the anti-oracle test's mechanism.
- `tests/test_checklist.py` — the route-set pin.

**Frontend — created**

- `frontend/src/pages/IntegrationsPage.tsx` — container: fetching, mutations, card layout.
- `frontend/src/pages/IntegrationsPage.test.tsx`
- `frontend/src/router.test.ts` — the frontend half of the dead-link pair.

**Frontend — modified**

- `frontend/src/api/types.ts` — `IntegrationsResponse` and friends.
- `frontend/src/api/client.ts` — four functions.
- `frontend/src/router.tsx` — the route.
- `frontend/src/Layout.tsx` — `isOrgAdmin` and the nav entry.

Presentational children (`IntegrationCard`, `ProviderForm`) live **inside** `IntegrationsPage.tsx` rather than in `components/`, matching `QboPage.tsx`, which keeps its table/panel/dialog in-file.

---

### Task 1: `oauth` flag on `ProviderSpec`

**Files:**
- Modify: `src/usali/integrations.py:56-81`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_integrations.py`:

```python
def test_only_qbo_is_an_oauth_provider():
    """The page branches on this flag instead of comparing against the string
    "qbo" in TypeScript. An EXACT set, so a second OAuth provider has to come
    here and be considered rather than silently rendering a credential form."""
    from usali.integrations import PROVIDERS

    assert [s.provider for s in PROVIDERS if s.oauth] == ["qbo"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_integrations.py::test_only_qbo_is_an_oauth_provider -v`
Expected: FAIL — `AttributeError: 'ProviderSpec' object has no attribute 'oauth'`

- [ ] **Step 3: Add the field**

In `src/usali/integrations.py`, add to `ProviderSpec` after `plain_fields`:

```python
    plain_fields: tuple[str, ...]
    # True when the credential is obtained by redirect rather than typed in.
    # The read endpoint serves this so the page renders a Connect button and
    # no inputs; without it the frontend would compare against "qbo", which
    # is the closed set restated in another language.
    oauth: bool = False
```

and set it on the QBO row only:

```python
    ProviderSpec(ACCOUNTING, "qbo", ("refresh_token",), ("realm_id",), oauth=True),
```

- [ ] **Step 4: Run it and watch it pass**

Run: `pytest tests/test_integrations.py::test_only_qbo_is_an_oauth_provider -v`
Expected: PASS

- [ ] **Step 5: Run the neighboring suites — a new dataclass field can break construction sites**

Run: `pytest tests/test_integrations.py tests/test_integrations_api.py -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/usali/integrations.py tests/test_integrations.py
git commit -m "feat(oh17): mark qbo as the one oauth provider"
```

---

### Task 2: A public `product_name()`

`_PRODUCT_NAMES` is private and its comment says "for refusal messages only … Never used as a key." The page needs "QuickBooks Online" rather than `"qbo"`. An accessor keeps the dict private and keeps the never-a-key half true.

**Files:**
- Modify: `src/usali/integrations.py:92-105`
- Test: `tests/test_integrations.py`

- [ ] **Step 1: Write the failing test**

```python
def test_every_provider_has_an_operator_facing_name():
    """A provider with no product name would render as its key on the
    integrations page. An exact pairing over PROVIDERS, so a sixth provider
    fails here rather than shipping a card labeled "adp2"."""
    from usali.integrations import PROVIDERS, product_name

    for spec in PROVIDERS:
        assert product_name(spec.provider) != spec.provider
```

- [ ] **Step 2: Run it and watch it fail**

Run: `pytest tests/test_integrations.py::test_every_provider_has_an_operator_facing_name -v`
Expected: FAIL — `ImportError: cannot import name 'product_name'`

- [ ] **Step 3: Add the accessor and correct the comment**

Replace the comment above `_PRODUCT_NAMES` with:

```python
# Operator-facing names, reached through `product_name` below. The row stores
# "qbo"; a hotel controller reads "QuickBooks Online". Never used as a key.
```

and add after the dict:

```python
def product_name(provider: str) -> str:
    """The operator-facing name for a provider key.

    Falls back to the key itself rather than raising: a missing name is a
    cosmetic defect on one card, not a reason to refuse the page. The
    fallback is what `test_every_provider_has_an_operator_facing_name`
    refuses to let ship."""
    return _PRODUCT_NAMES.get(provider, provider)
```

- [ ] **Step 4: Run it and watch it pass**

Run: `pytest tests/test_integrations.py::test_every_provider_has_an_operator_facing_name -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/usali/integrations.py tests/test_integrations.py
git commit -m "feat(oh17): expose provider product names through an accessor"
```

---

### Task 3: Serve the provider specs on `GET /api/integrations`

**Files:**
- Modify: `src/usali/integrations_api.py:86-97` (models) and `:109-160` (the handler)
- Test: `tests/test_integrations_api.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_integrations_api.py`:

```python
def test_the_read_serves_the_provider_specs(integrations_client):
    """Derived from PROVIDERS, never a hand-written list: a sixth provider
    needs no edit here, and a frontend copy of this data would have nothing
    checking it (see the design doc, section 3)."""
    body = integrations_client.get("/api/integrations").json()
    served = {
        item["integration"]: {p["provider"] for p in item["providers"]}
        for item in body["items"]
    }
    expected: dict[str, set[str]] = {}
    for spec in integrations.PROVIDERS:
        expected.setdefault(spec.integration, set()).add(spec.provider)
    assert served == expected


def test_each_served_field_is_flagged_secret_exactly_as_the_spec_says(
    integrations_client,
):
    body = integrations_client.get("/api/integrations").json()
    by_pair = {
        (item["integration"], p["provider"]): p
        for item in body["items"]
        for p in item["providers"]
    }
    for spec in integrations.PROVIDERS:
        served = by_pair[(spec.integration, spec.provider)]
        assert served["oauth"] is spec.oauth
        assert served["label"] == integrations.product_name(spec.provider)
        secret = {f["name"] for f in served["fields"] if f["secret"]}
        plain = {f["name"] for f in served["fields"] if not f["secret"]}
        assert secret == set(spec.secret_fields)
        assert plain == set(spec.plain_fields)


def test_the_provider_block_carries_no_secret_values(integrations_client):
    """The spec names the secret FIELDS; it must never carry their VALUES.
    Planted first so the assertion has something real to miss."""
    plant_credential(
        integrations_client, "payroll", "gusto",
        {"api_token": "tok-do-not-leak", "company_id": "c-1"},
    )
    raw = integrations_client.get("/api/integrations").text
    assert "tok-do-not-leak" not in raw
```

- [ ] **Step 2: Run them and watch them fail**

Run: `pytest tests/test_integrations_api.py -k "provider_specs or flagged_secret or no_secret_values" -v`
Expected: FAIL — `KeyError: 'providers'`

- [ ] **Step 3: Add the models**

In `src/usali/integrations_api.py`, above `class IntegrationModel`:

```python
class ProviderFieldModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    secret: bool


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    label: str
    oauth: bool
    fields: list[ProviderFieldModel]
```

and add to `IntegrationModel`, after `connected_at`:

```python
    # Every provider this integration accepts, so the page renders forms it
    # does not have a field list for. Derived from PROVIDERS on each read —
    # cheap, and it cannot go stale the way a module-level copy could.
    providers: list[ProviderModel]
```

- [ ] **Step 4: Build the block and attach it**

Add above `get_integrations`:

```python
def _providers_for(integration: str) -> list[ProviderModel]:
    """The provider specs the page renders, straight off PROVIDERS.

    `secret` is membership in `secret_fields`, not a second list: the two
    halves of `fields` are what the spec already distinguishes, and deriving
    the flag here is what stops a field being described as plain on the wire
    while sitting on an EncryptedString column."""
    return [
        ProviderModel(
            provider=spec.provider,
            label=product_name(spec.provider),
            oauth=spec.oauth,
            fields=[
                ProviderFieldModel(name=name, secret=name in spec.secret_fields)
                for name in spec.fields
            ],
        )
        for spec in PROVIDERS
        if spec.integration == integration
    ]
```

Import `PROVIDERS` and `product_name` from `usali.integrations` alongside the existing imports. Then in `get_integrations`, pass `providers=_providers_for(integration)` to **both** `IntegrationModel(...)` constructions — the `row is None` branch and the connected branch.

- [ ] **Step 5: Run them and watch them pass**

Run: `pytest tests/test_integrations_api.py -k "provider_specs or flagged_secret or no_secret_values" -v`
Expected: PASS

- [ ] **Step 6: Run the whole API suite — `extra="forbid"` makes a typo a 500, not a warning**

Run: `pytest tests/test_integrations_api.py -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/usali/integrations_api.py tests/test_integrations_api.py
git commit -m "feat(oh17): serve the provider specs on the integrations read"
```

---

### Task 4: The callback's failure paths redirect

**Files:**
- Modify: `src/usali/integrations_api.py:379-383` and `:517-607`
- Test: `tests/test_integrations_oauth.py:400-417`

- [ ] **Step 1: Rewrite the anti-oracle test onto `Location`**

Replace `test_every_bad_state_is_refused_the_exact_same_way` in `tests/test_integrations_oauth.py` with:

```python
def test_every_bad_state_is_refused_the_exact_same_way(oauth_client):
    """Forged, expired, malformed and MISSING are one refusal, byte for byte.
    The difference between them is an oracle about other tenants' in-flight
    grants — and "missing" is the one that slips: declared as a required query
    parameter it would be FastAPI's 422 naming the field, which no other
    failure mode produces.

    Compared on the redirect now rather than on a 400 body: the refusal moved
    to a Location header so an operator whose state expired at Intuit lands
    back on the page. The property is unchanged and so is what would break it
    — a per-variant message."""
    def _refusal(params):
        resp = oauth_client.get(_CALLBACK, params=params)
        return resp.status_code, resp.headers["location"]

    base = {"code": "good", "realmId": "r1"}
    forged = _refusal({**base, "state": "1:s:9999999999:" + "de" * 32})
    assert forged == (307, "/integrations?error=invalid+authorization+state")
    assert _refusal({**base, "state": "garbage"}) == forged
    assert _refusal({**base, "state": ""}) == forged
    assert _refusal(base) == forged                              # missing
    assert _refusal({**base, "state": sign_state(
        org_id=1, subject="s", now=time.time() - 3600)}) == forged   # expired
```

- [ ] **Step 2: Add the two tests for the valid-state failures**

```python
def test_a_refused_grant_redirects_with_intuits_own_words(oauth_client, monkeypatch):
    """Reachable only with a VALID signature, so the detail discloses nothing
    about another tenant — and it is the difference between "you declined" and
    "that code is already spent", which an operator needs."""
    def _refuse(code):
        raise QboError("access_denied: the user declined")
    oauth_client.app.state.exchange_qbo_code = _refuse

    resp = oauth_client.get(_CALLBACK, params={
        "code": "good", "realmId": "r1",
        "state": sign_state(org_id=1, subject="s"),
    })
    assert resp.status_code == 307
    assert "access_denied" in resp.headers["location"]


def test_no_redirect_carries_the_code_or_the_state(oauth_client):
    """The rule at the success redirect, applied to the failures: these travel
    through history and every proxy between here and the browser."""
    state = sign_state(org_id=1, subject="s")
    resp = oauth_client.get(_CALLBACK, params={
        "realmId": "r1", "state": state,          # no code -> failure branch
    })
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert state not in location
    assert "code=" not in location
```

Add `from usali.qbo_client import QboError` to the imports if it is not already there.

- [ ] **Step 3: Run them and watch them fail**

Run: `pytest tests/test_integrations_oauth.py -k "refused_the_exact_same_way or intuits_own_words or carries_the_code" -v`
Expected: FAIL — `KeyError: 'location'` (the responses are still 400s)

- [ ] **Step 4: Add the redirect helper**

In `src/usali/integrations_api.py`, replace the `_CONNECTED_REDIRECT` block with:

```python
# Where the callback lands the operator, win or lose. A SPA route, so the
# browser that followed Intuit's redirect ends up back in the connect UI with
# the result visible rather than looking at a JSON body.
_INTEGRATIONS_PATH = "/integrations"
_CONNECTED_REDIRECT = f"{_INTEGRATIONS_PATH}?connected=accounting"

# One string for every bad state. Forged, tampered, expired, malformed and
# absent must be indistinguishable — the property
# `test_every_bad_state_is_refused_the_exact_same_way` exists to hold — so the
# detail here is fixed and never names which check failed.
_BAD_STATE_DETAIL = "invalid authorization state"


def _error_redirect(detail: str) -> RedirectResponse:
    """A failed grant, handed back to the page instead of raised at the
    browser. Carries neither `code` nor `state`, for the reason the success
    redirect gives."""
    return RedirectResponse(
        url=f"{_INTEGRATIONS_PATH}?{urlencode({'error': detail})}",
        status_code=307,
    )
```

- [ ] **Step 5: Turn the three raises into returns**

In `callback`, replace each failure `raise` with the matching return:

```python
    verified = verify_state(state or "")
    if verified is None:
        return _error_redirect(_BAD_STATE_DETAIL)
    org_id, subject = verified
    if not code or not realm_id:
        # Only reachable by a caller holding a VALID state, so naming what is
        # missing discloses nothing. Real Intuit sends `error=access_denied`
        # here when the operator declines consent.
        return _error_redirect("QuickBooks returned no authorization code")
    try:
        refresh_token: str = request.app.state.exchange_qbo_code(code)
    except QboError as exc:
        return _error_redirect(f"QuickBooks refused the grant: {exc}")
```

Leave the `spec is None` branch as an `HTTPException` — it is a server misconfiguration, not an operator-facing outcome, and it is already `pragma: no cover`. Keep the long comment above the `QboError` branch where it is; only its final statement changes.

- [ ] **Step 6: Run them and watch them pass**

Run: `pytest tests/test_integrations_oauth.py -v`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/usali/integrations_api.py tests/test_integrations_oauth.py
git commit -m "fix(oh17): land a failed QBO grant back on the page"
```

---

### Task 5: Pin the full checklist route set

**Files:**
- Test: `tests/test_checklist.py` (append near `test_demand_feed_is_the_one_item_without_a_surface`)

- [ ] **Step 1: Write the test**

```python
def test_every_item_route_is_pinned():
    """The Python half of the dead-link pair. Its counterpart is
    `frontend/src/router.test.ts`, which asserts every path here is served by
    the SPA — neither test can see the other's language, so the set is pinned
    in both and a new `where` fails here until both move together.

    /integrations was in this list before it was a route, which is how the
    setup checklist shipped two links into nothing."""
    assert sorted({i.where for i in ITEMS if i.where is not None}) == [
        "/employees",
        "/integrations",
        "/property-config",
        "/upload",
    ]
```

- [ ] **Step 2: Run it**

Run: `pytest tests/test_checklist.py::test_every_item_route_is_pinned -v`
Expected: PASS immediately — it pins today's truth. If it fails, the sorted list in the test is wrong; correct the test to the actual set rather than changing `ITEMS`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_checklist.py
git commit -m "test(oh17): pin the full set of checklist routes"
```

---

### Task 6: Frontend types and client functions

**Files:**
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts`

- [ ] **Step 1: Add the types**

Append to `frontend/src/api/types.ts`:

```ts
/** One credential field a provider needs. A `secret` field is write-only:
 * `tests/test_integrations_api.py::test_the_provider_block_carries_no_secret_values`
 * is what holds the API to never returning its value, which is why an input
 * for one starts blank. */
export type ProviderField = {
  name: string
  secret: boolean
}

export type IntegrationProvider = {
  provider: string
  label: string
  /** Obtained by redirect, not typed in — the card offers a button, not
   * inputs. Which providers those are is closed in Python;
   * `tests/test_integrations.py::test_only_qbo_is_an_oauth_provider` pins
   * the set. */
  oauth: boolean
  fields: ProviderField[]
}

export type Integration = {
  integration: string
  connected: boolean
  provider: string | null
  /** Non-secret identifiers — a QBO realm, a Gusto company id. */
  identifiers: Record<string, string>
  connected_at: string | null
  providers: IntegrationProvider[]
}

export type IntegrationsResponse = {
  items: Integration[]
}

export type AuthorizeUrl = {
  url: string
}
```

- [ ] **Step 2: Add the client functions**

Append to `frontend/src/api/client.ts`:

```ts
// --- Integrations (OH-17) -----------------------------------------------------
// The gate is server-side — `require_grants(ORG_ADMIN)` in
// src/usali/integrations_api.py is where it is enforced, not here. The read
// carries the provider specs, so this client holds no credential field list
// of its own; `connect` sends whatever the spec named.

export function getIntegrations(): Promise<IntegrationsResponse> {
  return getJson('/api/integrations')
}

export async function connectIntegration(
  integration: string,
  body: { provider: string } & Record<string, string>,
): Promise<void> {
  const res = await fetch(`/api/integrations/${integration}`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res) // 422 carries the refusal wording
}

export async function disconnectIntegration(integration: string): Promise<void> {
  const res = await fetch(`/api/integrations/${integration}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}

export function getAuthorizeUrl(): Promise<AuthorizeUrl> {
  return getJson('/api/integrations/accounting/authorize')
}
```

Add `AuthorizeUrl` and `IntegrationsResponse` to the existing `import type { … } from './types'` block.

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx tsc -b --noEmit`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(oh17): integrations API client"
```

---

### Task 7: The route, the nav entry, and the frontend half of the dead-link pair

**Files:**
- Create: `frontend/src/router.test.ts`
- Modify: `frontend/src/router.tsx`, `frontend/src/Layout.tsx`

- [ ] **Step 1: Write the failing route test**

Create `frontend/src/router.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { isServedPath } from './router'

/**
 * The frontend half of the dead-link pair. Its counterpart is
 * `tests/test_checklist.py::test_every_item_route_is_pinned`, which pins the
 * same list in Python. Neither side can see the other's language, so both
 * pin it and a new checklist route fails the Python half first.
 *
 * These are the `where` values `usali.checklist.ITEMS` can return. A link to
 * a path the router does not serve renders as a dead link with no error —
 * which is how /integrations shipped on the checklist before it was a route.
 */
const CHECKLIST_ROUTES = [
  '/employees',
  '/integrations',
  '/property-config',
  '/upload',
]

describe('checklist routes', () => {
  it('are all served by the SPA', () => {
    for (const path of CHECKLIST_ROUTES) {
      expect(isServedPath(path), path).toBe(true)
    }
  })
})
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd frontend && npx vitest run src/router.test.ts`
Expected: FAIL — `expected false to be true` on `/integrations`

- [ ] **Step 3: Add the route**

In `frontend/src/router.tsx`, import the page beside the other page imports:

```ts
import IntegrationsPage from './pages/IntegrationsPage'
```

Add the search type near `CoverageSearch`:

```ts
/**
 * The QBO callback lands here with its result in the URL:
 * `?connected=accounting` on success, `?error=…` on a refused or expired
 * grant. `_CONNECTED_REDIRECT` and `_error_redirect` in
 * src/usali/integrations_api.py are where those two URLs are built. Both
 * params are cleared by the page once shown, so a reload does not
 * re-announce a connection that happened minutes ago.
 */
export type IntegrationsSearch = {
  connected?: string
  error?: string
}
```

and the route beside the others:

```ts
const integrationsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/integrations',
  component: IntegrationsPage,
  validateSearch: (search: Record<string, unknown>): IntegrationsSearch => ({
    connected: optionalString(search.connected),
    error: optionalString(search.error),
  }),
})
```

Add `integrationsRoute` to the `childRoutes` array. `servedPaths` reads that same array, so the test passes without a second registration.

- [ ] **Step 4: Create a placeholder page so the import resolves**

Create `frontend/src/pages/IntegrationsPage.tsx`:

```tsx
export default function IntegrationsPage() {
  return null
}
```

Task 8 replaces this entirely.

- [ ] **Step 5: Run it and watch it pass**

Run: `cd frontend && npx vitest run src/router.test.ts`
Expected: PASS

- [ ] **Step 6: Add the nav entry**

In `frontend/src/Layout.tsx`, beside the other predicates at line 83:

```ts
// org_admin ONLY: the API is require_grants(ORG_ADMIN), stricter than
// isScheduler, and a nav entry is a promise about what this account can do.
const isOrgAdmin = (me: Me | undefined) => hasRole(me, 'org_admin')
```

and in the `Accounting` section's `items`, after the `/qbo` entry:

```ts
      { to: '/integrations', label: 'Integrations', icon: PlugIcon, show: isOrgAdmin },
```

If `PlugIcon` does not exist in `frontend/src/components/icons.tsx`, use `SyncIcon` — already imported for `/qbo` — rather than adding an icon in this task.

- [ ] **Step 7: Typecheck and run the frontend suite**

Run: `cd frontend && npx tsc -b --noEmit && npm test`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add frontend/src/router.tsx frontend/src/router.test.ts frontend/src/Layout.tsx frontend/src/pages/IntegrationsPage.tsx
git commit -m "feat(oh17): route and nav entry for /integrations"
```

---

### Task 8: The page — read, cards, and the 503 refusal

**Files:**
- Modify: `frontend/src/pages/IntegrationsPage.tsx`
- Create: `frontend/src/pages/IntegrationsPage.test.tsx`

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/IntegrationsPage.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '../auth/authContext'
import { AUTHED_CONTEXT } from '../test/fixtures'
import { createAppRouter } from '../router'

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  getIntegrations: vi.fn(),
  connectIntegration: vi.fn(),
  disconnectIntegration: vi.fn(),
  getAuthorizeUrl: vi.fn(),
  getMe: vi.fn(),
}))
import { ApiError, getIntegrations, getMe } from '../api/client'
import type { Integration } from '../api/types'

function gusto(overrides: Partial<Integration> = {}): Integration {
  return {
    integration: 'payroll',
    connected: false,
    provider: null,
    identifiers: {},
    connected_at: null,
    providers: [{
      provider: 'gusto',
      label: 'Gusto',
      oauth: false,
      fields: [
        { name: 'api_token', secret: true },
        { name: 'company_id', secret: false },
      ],
    }],
    ...overrides,
  }
}

function qbo(overrides: Partial<Integration> = {}): Integration {
  return {
    integration: 'accounting',
    connected: false,
    provider: null,
    identifiers: {},
    connected_at: null,
    providers: [{
      provider: 'qbo',
      label: 'QuickBooks Online',
      oauth: true,
      fields: [
        { name: 'refresh_token', secret: true },
        { name: 'realm_id', secret: false },
      ],
    }],
    ...overrides,
  }
}

function renderPage(entry = '/integrations') {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [entry] }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <AuthContext.Provider value={AUTHED_CONTEXT}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

describe('IntegrationsPage', () => {
  beforeEach(() => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
  })

  it('shows a connected integration with its identifier', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [qbo({
        connected: true,
        provider: 'qbo',
        identifiers: { realm_id: '4620816365' },
        connected_at: '2026-08-31T10:00:00',
      })],
    })
    renderPage()
    expect(await screen.findByText('QuickBooks Online')).toBeInTheDocument()
    expect(screen.getByText('4620816365')).toBeInTheDocument()
  })

  it('refuses the whole page when a credential cannot be decrypted', async () => {
    vi.mocked(getIntegrations).mockRejectedValue(
      new ApiError(503, 'the accounting credential cannot be decrypted'),
    )
    renderPage()
    expect(
      await screen.findByText(/cannot be decrypted/),
    ).toBeInTheDocument()
    // The other cards must NOT render as "not connected" beside it: that is
    // the lie CredentialUnreadable exists to prevent, told on the one page
    // someone came to for an explanation.
    expect(screen.queryByText('Gusto')).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: FAIL — nothing rendered; the page returns `null`

- [ ] **Step 3: Write the page**

Replace `frontend/src/pages/IntegrationsPage.tsx` entirely:

```tsx
// Per-tenant integration config (OH-17). One query for the page; the cards
// below are presentational. The provider field lists come from the API — this
// file must never grow one of its own, or it becomes a second copy of
// PROVIDERS with nothing checking it (design doc, section 3).

import { useQuery } from '@tanstack/react-query'

import { ApiError, getIntegrations } from '../api/client'
import type { Integration } from '../api/types'
import { Card, PageHeader } from '../components/ui'
import { errorMessage } from '../lib/errors'

const TITLES: Record<string, string> = {
  payroll: 'Payroll',
  accounting: 'Accounting',
  demand_feed: 'Demand feed',
}

function IntegrationCard({ item }: { item: Integration }) {
  const title = TITLES[item.integration] ?? item.integration
  const connected = item.providers.find((p) => p.provider === item.provider)
  return (
    <Card>
      <h2 className="text-sm font-semibold">{title}</h2>
      {item.connected && connected !== undefined ? (
        <div className="mt-2 space-y-1 text-sm">
          <p>{connected.label}</p>
          {Object.entries(item.identifiers).map(([name, value]) => (
            <p key={name} className="text-ink-muted">
              {name}: <span className="tabular-nums">{value}</span>
            </p>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-sm text-ink-muted">Not connected</p>
      )}
    </Card>
  )
}

export default function IntegrationsPage() {
  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: getIntegrations,
  })

  // A 503 here is CredentialUnreadable, raised by `get_integrations` in
  // src/usali/integrations_api.py — that is where the whole read is refused
  // rather than an undecryptable row being reported as disconnected. This
  // branch is the frontend half of that refusal: rendering the readable
  // cards beside the message would restore the lie the API declined to
  // tell. Pinned by 'refuses the whole page when a credential cannot be
  // decrypted' in this file's test.
  if (integrations.error instanceof ApiError && integrations.error.status === 503) {
    return (
      <>
        <PageHeader title="Integrations" />
        <Card>
          <p className="text-sm">{integrations.error.detail}</p>
        </Card>
      </>
    )
  }

  return (
    <>
      <PageHeader title="Integrations" />
      {integrations.error !== null && integrations.error !== undefined && (
        <Card><p className="text-sm">{errorMessage(integrations.error)}</p></Card>
      )}
      <div className="space-y-3">
        {(integrations.data?.items ?? []).map((item) => (
          <IntegrationCard key={item.integration} item={item} />
        ))}
      </div>
    </>
  )
}
```

`PageHeader` takes `title: string` and `Card` takes `children`, both confirmed against `frontend/src/components/ui.tsx:19-56`.

- [ ] **Step 4: Run them and watch them pass**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/IntegrationsPage.tsx frontend/src/pages/IntegrationsPage.test.tsx
git commit -m "feat(oh17): integrations page read surface"
```

---

### Task 9: The connect form

**Files:**
- Modify: `frontend/src/pages/IntegrationsPage.tsx`, `frontend/src/pages/IntegrationsPage.test.tsx`

- [ ] **Step 1: Write the failing tests**

Append inside the `describe` block:

```tsx
  it('renders an input per spec field and sends what it collected', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [gusto()] })
    vi.mocked(connectIntegration).mockResolvedValue(undefined)
    renderPage()

    const token = await screen.findByLabelText('api_token')
    // Secret fields are password inputs and start empty even when connected:
    // the API never returns a value, and PUT is a full replace.
    expect(token).toHaveAttribute('type', 'password')
    expect(token).toHaveValue('')

    fireEvent.change(token, { target: { value: 'tok-1' } })
    fireEvent.change(screen.getByLabelText('company_id'), {
      target: { value: 'c-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect Gusto' }))

    await waitFor(() => {
      expect(connectIntegration).toHaveBeenCalledWith('payroll', {
        provider: 'gusto', api_token: 'tok-1', company_id: 'c-1',
      })
    })
  })

  it('shows the backend refusal verbatim', async () => {
    // The demand feed's crm_ref rule lives in verify_credentials and is NOT
    // restated here: this asserts the page relays it, not that it knows it.
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [gusto({ integration: 'demand_feed', providers: [{
        provider: 'delphi', label: 'Delphi', oauth: false,
        fields: [{ name: 'subscription_key', secret: true }],
      }] })],
    })
    vi.mocked(connectIntegration).mockRejectedValue(new ApiError(
      422, 'no property in this workspace has a crm_ref, so the demand feed cannot be verified',
    ))
    renderPage()

    fireEvent.change(await screen.findByLabelText('subscription_key'), {
      target: { value: 'k-1' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect Delphi' }))

    expect(await screen.findByText(/has a crm_ref/)).toBeInTheDocument()
  })
```

Add `fireEvent` and `waitFor` to the `@testing-library/react` import and `connectIntegration` to the client import.

- [ ] **Step 2: Run them and watch them fail**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: FAIL — `Unable to find a label with the text of: api_token`

- [ ] **Step 3: Add `ProviderForm` and wire the mutation**

Add to `IntegrationsPage.tsx`, above `IntegrationCard`:

```tsx
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { connectIntegration } from '../api/client'
import type { IntegrationProvider } from '../api/types'
import { controlClass } from '../components/ui'

/** Renders whatever fields the spec named. It has no list of its own — that
 * is the point of serving the specs. `aria-label` rather than a wrapping
 * <label>, because the accessible name is what the tests query by:
 * 'renders an input per spec field' in IntegrationsPage.test.tsx is where
 * that name is exercised. */
function ProviderForm({
  integration, spec, onDone,
}: {
  integration: string
  spec: IntegrationProvider
  onDone: () => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const connect = useMutation({
    mutationFn: () => connectIntegration(integration, { provider: spec.provider, ...values }),
    onSuccess: onDone,
  })
  return (
    <form
      className="mt-2 space-y-2"
      onSubmit={(e) => { e.preventDefault(); connect.mutate() }}
    >
      {spec.fields.map((field) => (
        <input
          key={field.name}
          aria-label={field.name}
          type={field.secret ? 'password' : 'text'}
          className={controlClass}
          value={values[field.name] ?? ''}
          onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
        />
      ))}
      <button type="submit" className={controlClass}>{`Connect ${spec.label}`}</button>
      {connect.error !== null && (
        <p className="text-sm text-danger-red">{errorMessage(connect.error)}</p>
      )}
    </form>
  )
}
```

Then thread it through `IntegrationCard`, replacing the `Not connected` paragraph:

```tsx
function IntegrationCard({ item, onDone }: { item: Integration; onDone: () => void }) {
  const title = TITLES[item.integration] ?? item.integration
  const connected = item.providers.find((p) => p.provider === item.provider)
  return (
    <Card>
      <h2 className="text-sm font-semibold">{title}</h2>
      {item.connected && connected !== undefined ? (
        <div className="mt-2 space-y-1 text-sm">
          <p>{connected.label}</p>
          {Object.entries(item.identifiers).map(([name, value]) => (
            <p key={name} className="text-ink-muted">
              {name}: <span className="tabular-nums">{value}</span>
            </p>
          ))}
        </div>
      ) : (
        <>
          <p className="mt-2 text-sm text-ink-muted">Not connected</p>
          {item.providers.filter((spec) => !spec.oauth).map((spec) => (
            <ProviderForm
              key={spec.provider}
              integration={item.integration}
              spec={spec}
              onDone={onDone}
            />
          ))}
        </>
      )}
    </Card>
  )
}
```

and in the page body, pass the invalidation down:

```tsx
  const qc = useQueryClient()
  const onDone = () => { void qc.invalidateQueries({ queryKey: ['integrations'] }) }
```

with `<IntegrationCard key={item.integration} item={item} onDone={onDone} />`.

- [ ] **Step 4: Run them and watch them pass**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/IntegrationsPage.tsx frontend/src/pages/IntegrationsPage.test.tsx
git commit -m "feat(oh17): connect a credential-based integration"
```

---

### Task 10: OAuth connect and the callback's search params

**Files:**
- Modify: `frontend/src/pages/IntegrationsPage.tsx`, `frontend/src/pages/IntegrationsPage.test.tsx`

- [ ] **Step 1: Write the failing tests**

```tsx
  it('sends an oauth provider to the consent URL and renders no inputs', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [qbo()] })
    vi.mocked(getAuthorizeUrl).mockResolvedValue({ url: 'https://intuit.test/consent' })
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    renderPage()

    expect(await screen.findByRole('button', { name: 'Connect QuickBooks Online' }))
      .toBeInTheDocument()
    // No credential inputs: the tokens come back from Intuit, not the operator.
    expect(screen.queryByLabelText('refresh_token')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Connect QuickBooks Online' }))
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith('https://intuit.test/consent')
    })
    vi.unstubAllGlobals()
  })

  it('announces a completed grant and clears the param', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [qbo()] })
    renderPage('/integrations?connected=accounting')
    expect(await screen.findByText(/QuickBooks Online is connected/)).toBeInTheDocument()
    await waitFor(() => {
      expect(window.location.search).not.toContain('connected=')
    })
  })

  it('renders a failed grant on the accounting card', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({ items: [qbo()] })
    renderPage('/integrations?error=QuickBooks+refused+the+grant%3A+access_denied')
    expect(await screen.findByText(/access_denied/)).toBeInTheDocument()
  })
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: FAIL — no Connect button for the OAuth card

- [ ] **Step 3: Implement**

In `IntegrationsPage.tsx`:

```tsx
import { getRouteApi } from '@tanstack/react-router'
import { getAuthorizeUrl } from '../api/client'

const route = getRouteApi('/integrations')
```

Add an OAuth branch in `IntegrationCard`'s not-connected path — for a spec with `oauth: true`, render a button instead of a form:

```tsx
function OauthConnect({ spec }: { spec: IntegrationProvider }) {
  // A top-level navigation. The authorize endpoint hands back a URL instead
  // of a 302 so that this, and not the fetch seam in api/client.ts, is what
  // leaves the origin — its docstring in src/usali/integrations_api.py is
  // where that reasoning lives.
  const start = useMutation({
    mutationFn: getAuthorizeUrl,
    onSuccess: (res) => { window.location.assign(res.url) },
  })
  return (
    <div className="mt-2 space-y-2">
      <button type="button" className={controlClass} onClick={() => start.mutate()}>
        {`Connect ${spec.label}`}
      </button>
      {start.error !== null && (
        <p className="text-sm text-danger-red">{errorMessage(start.error)}</p>
      )}
    </div>
  )
}
```

In the page body, read and clear the params:

```tsx
  const search = route.useSearch()
  const navigate = route.useNavigate()
  useEffect(() => {
    // Shown once. Cleared with `replace` so a reload does not re-announce a
    // grant that completed minutes ago, and so Back does not walk into it.
    if (search.connected !== undefined || search.error !== undefined) {
      void navigate({ search: {}, replace: true })
    }
  }, [search.connected, search.error, navigate])
```

The clearing navigation empties `search` on the next render, so the note has
to be captured before it goes:

```tsx
  // Captured on first render: `navigate` above empties `search`, and reading
  // the note from `search` afterwards would blank it the instant it appeared.
  const landed = useRef({ connected: search.connected, error: search.error })
```

Render it on the matching card by passing two more props to `IntegrationCard`:

```tsx
        <IntegrationCard
          key={item.integration}
          item={item}
          onDone={onDone}
          note={landed.current.connected === item.integration
            ? `${item.providers[0]?.label ?? item.integration} is connected.`
            : undefined}
          error={landed.current.connected === item.integration
            || (item.integration === 'accounting' && landed.current.error !== undefined)
            ? landed.current.error
            : undefined}
        />
```

and rendering them at the top of the card body:

```tsx
      {note !== undefined && <p className="mt-2 text-sm">{note}</p>}
      {error !== undefined && <p className="mt-2 text-sm text-danger-red">{error}</p>}
```

`error` lands on the accounting card because the callback is the only thing
that sets it, and it is the accounting integration's callback.

- [ ] **Step 4: Run them and watch them pass**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/IntegrationsPage.tsx frontend/src/pages/IntegrationsPage.test.tsx
git commit -m "feat(oh17): QBO consent round trip on the page"
```

---

### Task 11: Disconnect, behind a confirm

**Files:**
- Modify: `frontend/src/pages/IntegrationsPage.tsx`, `frontend/src/pages/IntegrationsPage.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
  it('disconnects only after the confirm names what is going', async () => {
    vi.mocked(getIntegrations).mockResolvedValue({
      items: [qbo({
        connected: true, provider: 'qbo',
        identifiers: { realm_id: '4620816365' },
        connected_at: '2026-08-31T10:00:00',
      })],
    })
    vi.mocked(disconnectIntegration).mockResolvedValue(undefined)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(disconnectIntegration).not.toHaveBeenCalled()
    // The confirm restates the identifier, so an operator with two QuickBooks
    // companies can tell which one they are about to drop.
    expect(screen.getByText(/4620816365/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Yes, disconnect' }))
    await waitFor(() => {
      expect(disconnectIntegration).toHaveBeenCalledWith('accounting')
    })
  })
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: FAIL — no Disconnect button

- [ ] **Step 3: Implement**

Add to the connected branch, after the identifiers:

```tsx
function ConnectedActions({
  item, spec, onDone,
}: {
  item: Integration
  spec: IntegrationProvider
  onDone: () => void
}) {
  const [confirming, setConfirming] = useState(false)
  const [replacing, setReplacing] = useState(false)
  const drop = useMutation({
    mutationFn: () => disconnectIntegration(item.integration),
    onSuccess: () => { setConfirming(false); onDone() },
  })
  const identifiers = Object.values(item.identifiers).join(', ')
  return (
    <div className="mt-3 space-y-2">
      <button type="button" className={controlClass}
              onClick={() => setReplacing((r) => !r)}>
        Replace credentials
      </button>
      {/* Re-connecting is the identical call the disconnected card makes —
          no second code path. `_store_credential` in
          src/usali/integrations_api.py is where the replace is made total,
          nulling every column the chosen provider does not use. */}
      {replacing && (spec.oauth
        ? <OauthConnect spec={spec} />
        : <ProviderForm integration={item.integration} spec={spec} onDone={onDone} />)}
      {confirming ? (
        <div className="space-y-2">
          {/* Names the identifier: an operator with two QuickBooks companies
              has to be able to tell which one they are dropping. */}
          <p className="text-sm">
            {`Disconnect ${spec.label}${identifiers === '' ? '' : ` (${identifiers})`}?`}
          </p>
          <button type="button" className={controlClass}
                  onClick={() => drop.mutate()}>
            Yes, disconnect
          </button>
          <button type="button" className={controlClass}
                  onClick={() => setConfirming(false)}>
            Cancel
          </button>
          {drop.error !== null && (
            <p className="text-sm text-danger-red">{errorMessage(drop.error)}</p>
          )}
        </div>
      ) : (
        <button type="button" className={controlClass}
                onClick={() => setConfirming(true)}>
          Disconnect
        </button>
      )}
    </div>
  )
}
```

and render `<ConnectedActions item={item} spec={connected} onDone={onDone} />` inside the connected branch, where `connected` is the spec already looked up at the top of `IntegrationCard`.

- [ ] **Step 4: Run it and watch it pass**

Run: `cd frontend && npx vitest run src/pages/IntegrationsPage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/IntegrationsPage.tsx frontend/src/pages/IntegrationsPage.test.tsx
git commit -m "feat(oh17): disconnect and replace credentials"
```

---

### Task 12: Nav gating test and the full suites

**Files:**
- Modify: `frontend/src/App.test.tsx` — the nav-role tests already live there (`shows Employees link for org_admin`, `shows Weekly Schedule link for property_gm`), and they use a `renderApp` helper this page's own test file does not have.

- [ ] **Step 1: Write the nav tests, matching the house pattern at `App.test.tsx:123-140`**

```tsx
  it('shows Integrations link for org_admin', async () => {
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['org_admin'] })
    renderApp()
    expect(await screen.findByRole('link', { name: 'Integrations' })).toBeInTheDocument()
  })

  it('hides Integrations link from a property_gm', async () => {
    // The strongest non-admin the system has, and still not an org_admin:
    // connecting a tenant's payroll is not a GM's call, and a nav entry is a
    // promise about what this account can do.
    vi.mocked(getMe).mockResolvedValue({ subject: 's', username: 'u', roles: ['property_gm'] })
    renderApp()
    await screen.findByRole('link', { name: 'SOS' }) // nav rendered
    expect(screen.queryByRole('link', { name: 'Integrations' })).not.toBeInTheDocument()
  })
```

- [ ] **Step 2: Run them**

Run: `cd frontend && npx vitest run src/App.test.tsx`
Expected: PASS (Task 7 already gated it) — if the second FAILS, `isOrgAdmin` was not applied to the nav item; fix that.

- [ ] **Step 3: Run everything**

Run: `pytest -q`
Expected: all pass — a full run, not targeted modules. Two of the eight OH-17 fixes broke distant callers that only the whole suite caught.

Run: `cd frontend && npm test && npx tsc -b --noEmit && npm run lint`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test(oh17): nav gating for the integrations entry"
```

- [ ] **Step 5: Update the roadmap**

`.github/roadmap.yml:208` still reads `status: in-progress` with the comment "no /integrations frontend page yet, so no one can actually connect from the app". That is now false. Change to `status: shipped` and remove the stale half of the comment — the roadmap entry promised connecting from inside the app, and that is what this delivers.

```bash
git add .github/roadmap.yml
git commit -m "docs(oh17): OH-17 ships"
```

---

## Verification

- `pytest -q` — full backend suite green.
- `cd frontend && npm test` — full frontend suite green.
- `npx tsc -b --noEmit` and `npm run lint` clean.
- Manual: as an `org_admin` on a fresh org, both `/setup` integration links land on the page rather than a Not Found. That is the defect this slice exists to close, and no automated test in this plan clicks a real link.
