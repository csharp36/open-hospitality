# The `/integrations` page

**Status:** design, 2026-08-31
**Roadmap:** OH-17, the slice that moves it off `in-progress`
**Depends on:** the OH-17 backend, merged in #108

## 1. Why this exists

OH-17's summary in `.github/roadmap.yml:200` promises that each hotel connects
its own QuickBooks, Gusto or ADP account **from inside the app**. The storage,
the resolution seam, the OAuth pair and a read-only `verify()` on all four
adapters all shipped. Nobody can connect. The roadmap entry says `in-progress`
for exactly that reason.

There is also a live defect. `src/usali/checklist.py:224` and `:229` set
`where="/integrations"` on the payroll and accounting items, and
`frontend/src/pages/ChecklistPage.tsx:151` renders that as a `<Link to=...>`.
There is no `/integrations` in `frontend/src/router.tsx`. **The setup checklist
on main has two links into nothing.** This slice is corrective, not only
additive, and §5.1 makes that failure detectable in future.

## 2. Scope

**In:** connect, re-connect and disconnect for all three integrations; the QBO
OAuth round trip including its failure paths; display of the non-secret
identifiers; the route and its nav entry.

**Out, deliberately:** a user-triggered `verify()` button, and any surfacing of
`integration_connected` audit events. Both are real and both were considered.
`verify()` runs today only as part of a write (`integrations_api.py:190`), so
exposing it needs an endpoint; the audit trail needs a read API. Neither is
required to close the two dead links or to let a hotel connect, which is what
`in-progress` is waiting on.

`realm_id` display is IN, and is not decoration. D-OH17.11 accepted the OAuth
`state` residual: a captured, unexpired state paired with an attacker's own
fresh Intuit code binds their QuickBooks company onto the victim org's row.
With that accepted, the displayed `realm_id` and the `integration_connected`
audit event are the only two signals separating a hijack from a normal
connection, and this page is where the first of them becomes visible at all.

## 3. Where the provider field lists live

The page must render, per provider, which credential fields to collect and
which of them are secret. That list exists once already, in `PROVIDERS`
(`src/usali/integrations.py:75`, and `ProviderSpec` at `:56`).

**Decision: the API serves the specs; the frontend holds no field list.**

The alternative — a `providers.ts` in the frontend repeating the five
provider/field combinations — is a hand-written second copy of `PROVIDERS`, and
this repo has already ruled on that exact question one layer down.
`ALL_CREDENTIAL_FIELDS` (`integrations.py:87`) is derived rather than repeated,
with the reason stated there: a hand-written second list is the drift the DB
CHECK exists to catch, and it would be caught only at the DB, one layer too
late for a good error. A frontend copy is the same list again, one layer
further out, where **nothing** checks it. Add a sixth provider and the page
silently cannot connect it, with no failing test anywhere.

Serving the specs also carries `_PRODUCT_NAMES` outward, so a card reads
"QuickBooks Online" without the frontend deriving a label from `"qbo"`.

### 3.1 The `oauth` flag

`ProviderSpec` gains `oauth: bool = False`, `True` on the QBO entry only. The
page branches on that flag rather than on `provider === 'qbo'`. This is §2.4 of
the comment-claims invariant applied: the closed set stays closed in code
instead of being restated as a string comparison in another language.

### 3.2 The shape

```python
class ProviderFieldModel(BaseModel):   # name, secret
class ProviderModel(BaseModel):        # provider, label, oauth, fields
# IntegrationModel gains:  providers: list[ProviderModel]
```

Built by filtering `PROVIDERS` on the integration; `secret` is membership in
`secret_fields`; `label` from `_PRODUCT_NAMES`. It rides on the existing `GET`
— no new endpoint.

Promoting `_PRODUCT_NAMES` makes its own comment partly false: it currently
reads "for refusal messages only … Never used as a key." The "never a key" half
stays true and still matters; the "refusal messages only" half stops being. It
is updated in the same commit.

## 4. The QBO round trip

Success is already decided by the backend: the callback 307s to
`/integrations?connected=accounting` (`integrations_api.py:383`). The page
handles that param.

Failure is not. Every failure path in `callback` raises `HTTPException`, which
an operator meets as a raw JSON 400 in a top-level window — including the most
ordinary non-success path there is, declining consent at Intuit.

**Decision: failures redirect too, in TWO shapes.**

`test_integrations_oauth.py:400` pins an anti-oracle property: forged, tampered,
expired, malformed and absent states are all refused byte-identically, so that
nothing distinguishes "you sent nothing" from "your state did not verify". A
single error redirect carrying a detail would break that. So:

| Case | Result |
|---|---|
| Any bad state | 307 to a **fixed** `?error=` string, identical for every variant |
| Valid state, grant refused | 307 carrying Intuit's fault text |

The second is reachable only by a caller holding a valid signature, and the
distinction an operator needs — "you declined" versus "that code is already
spent" — lives there. Neither redirect carries `code` or `state`, holding the
rule stated at `integrations_api.py:604`.

An operator whose state expired while they were at Intuit lands back on the
page rather than on JSON, which is the main reason bad states redirect at all.

## 5. The page

**Route.** `/integrations`, search params `connected?: string` and
`error?: string`.

**Nav.** In the Accounting section beside `/qbo`, gated by a new
`isOrgAdmin = (me) => hasRole(me, 'org_admin')` alongside the existing
`isScheduler` and `isPayroll` in `frontend/src/Layout.tsx:83`. The API is
`require_grants(ORG_ADMIN)` (`integrations_api.py:73`), so `isScheduler` —
which admits `property_gm` — would over-promise. `Layout.tsx:110` states the
rule this follows: a nav entry is a promise about what this account can do.

**Components.** `IntegrationsPage.tsx` owns every fetch and mutation; children
are presentational, the split `QboPage.tsx` states for itself.

- `IntegrationCard`, one per item, in `INTEGRATIONS` order.
- `ProviderForm`, rendering `ProviderModel.fields` generically: `secret: true`
  becomes `type="password"`. It holds no field list of its own.

**Connected state** shows the product label, the `identifiers` map, the
`connected_at`, a **Replace credentials** control that reopens the form, and
**Disconnect** behind a confirm dialog naming the integration and identifier
(the precedent is `QboPage`'s push confirm).

**Secret inputs are empty on mount even when connected.** The API never returns
a secret and `PUT` is a full replace, so a blank box is the truth; a masked
placeholder would imply a round-trip that does not happen.

**OAuth cards render no inputs.** Connect calls `GET /accounting/authorize` and
does a top-level `window.location.assign` on the returned URL — the seam that
endpoint's docstring says it returns a URL rather than a 302 in order to serve.

**The demand feed shows its form like any other.** `PUT /demand_feed` is a real
route (Delphi and Tripleseat are both in `PROVIDERS`) that `verify_credentials`
refuses without a `crm_ref`, by name. Showing the form and letting that 422
speak means an org whose `crm_ref` support has seeded can self-serve
immediately; a card with no inputs would leave them stranded, since the
checklist's `where=None` is about nobody being able to *complete* the item, not
about the credential being unenterable. The rule is not restated in the
frontend.

### 5.1 Error handling

- **503** from the `GET` refuses the **whole page** and prints the
  `CredentialUnreadable` message by name. Not a per-card empty state: the
  reasoning at `integrations_api.py:126` is that rendering the readable
  integrations while one is undecryptable is the lie the exception exists to
  prevent, told on the surface someone came to for an explanation. The remedy
  the message names still works while the page refuses, because `connect`
  upserts without reading the old row.
- **422** from connect renders inline on the form. This carries the demand
  feed's `crm_ref` refusal, missing and unknown fields, and a bad
  provider/integration pair.
- `?connected=accounting` shows a note on that card, then clears itself with a
  `replace` navigation so a reload does not re-announce an old connection.

Controls take `aria-label`s rather than `sr-only` spans: the accessible name
has to hold up under jsdom, which computes it differently from a browser.

## 6. Tests

### 6.1 The dead-link pair

Nothing today catches a `where` pointing at a route that does not exist. It is
a cross-boundary claim, discharged by relocation into two named halves that can
each see only their own language:

- `tests/test_checklist.py::test_every_item_route_is_pinned` — pins the full
  set of non-null `where` values as a literal (`/upload`, `/property-config`,
  `/integrations`, `/employees`), failing in both directions, as
  `test_demand_feed_is_the_one_item_without_a_surface` already does for the
  null half.
- `frontend/src/router.test.ts::test_every_checklist_route_exists` — asserts
  each of those paths resolves in the route tree, naming its Python
  counterpart in a comment so the pair is greppable from either side.

A fifth `where` fails the Python half until both move together.

### 6.2 Backend

- The served provider specs equal the ones computed from `PROVIDERS`, so a
  sixth provider needs no test edit and a hand-written list fails.
- `oauth: true` on QBO and on nothing else.
- No secret value appears anywhere in the `GET` payload, extended to cover the
  new `providers` block.
- `test_every_bad_state_is_refused_the_exact_same_way` compares full `Location`
  headers across all five bad-state variants — same property, new mechanism.
- A valid-state grant refusal redirects with Intuit's detail and carries
  neither `code` nor `state`.

### 6.3 Frontend

Following `PropertyConfigPage.test.tsx`: `vi.mock('../api/client')`,
`createAppRouter` over a memory history, `AUTHED_CONTEXT`.

- 503 refuses the whole page and names the rotation.
- A connected card shows its identifier and an empty secret input.
- The QBO card renders no text inputs; Connect calls the authorize endpoint.
- The demand-feed 422 surfaces the backend's `crm_ref` wording verbatim.
- `?connected=accounting` shows the note and clears the param; `?error=`
  renders on the accounting card.
- The nav entry is absent for a `property_gm`, present for an `org_admin`.

`beforeEach` bodies stay braced rather than concise arrows, so a returned mock
instance cannot turn a handled rejection into a phantom failure.

## 7. How we would know it is not working

1. **The checklist links still dead-end.** Direct check: click both from
   `/setup` as an `org_admin` on a fresh org.
2. **A hotel still cannot connect without support.** The demand feed is the
   honest exception and says so in its own words; payroll and accounting are
   not, and if either still needs an operator, this slice failed.
3. **A sixth provider needs a frontend edit.** If adding one to `PROVIDERS`
   requires touching TypeScript, §3 was not actually achieved and the second
   list came back.
