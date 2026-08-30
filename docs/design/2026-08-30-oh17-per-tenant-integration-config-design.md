# OH-17 — per-tenant integration config and the connect surfaces (design)

Status: **APPROVED — scope FINAL (2026-08-30).** Item 2 of
[`docs/ROADMAP.md`](../ROADMAP.md) §6, the largest hidden lift on the list
(§2.1). Consumes the open-items container shipped as
[OH-18 / B4](2026-08-30-track-b-b4-open-items-checklist-design.md) and settles
ROADMAP §7 open decision 3 (per-tenant secret storage shape).

Inherits [D8.1](2026-08-16-data-posture-progressive-onboarding-design.md) (one
always-sensitive data posture), **D8.3** (an unconfigured integration is *off*,
never mock), and **D-B4.1** (status is derived, never stored).

## 1. Goal

Let a tenant connect **their own** payroll, accounting, and demand-feed
accounts, and let the checklist tell the truth about whether they did.

Today `config.py:34-61` holds `qbo_*`, `payroll_provider`, `gusto_*`, `adp_*`,
`delphi_subscription_key` and `tripleseat_api_key` as process-wide `Settings` —
one value for the whole deployment. Connecting QuickBooks for tenant A would
change it for every tenant, so **"does QBO exist for this tenant?" is currently
unanswerable**, and `_probe_payroll` / `_probe_accounting`
(`checklist.py:167`, `:174`) return `False` unconditionally because that is the
only honest answer available (D-B4.3).

Success condition: the three integration checklist items become closeable by
the tenant, through a surface that cannot report `done` over an integration
that would fail on first use.

## 2. Decisions log

- **D-OH17.1 — The credential row IS the connection (CONFIRMED 2026-08-30).**
  Provider selection and credentials live on **one row**, so they cannot drift.
  A tenant structurally cannot pick a provider without supplying credentials
  for it, and cannot hold credentials for a provider they have not picked.

  This is the decision the rest hangs from. Today `OrgSettings.crm_provider`
  (`models.py:504`) says *which* provider while `Settings` holds its *keys* —
  two places, one of which is not even per-tenant. A per-tenant provider picker
  without per-tenant credentials would flip `demand_feed` to `done` over a pull
  that still 503s: the exact drift D-B4.1 and D8.3 exist to prevent, merely
  relocated. Putting both on one row makes the invariant structural rather
  than a matter of discipline.

  **Consequence: `OrgSettings` is dropped entirely.** `crm_provider` was its
  only column; migrating that column into the credential row leaves an empty
  table, and an empty table is a place for the next drift to grow back.

- **D-OH17.2 — ADR-005 symmetric field encryption, not ADR-004 HPKE
  (CONFIRMED 2026-08-30, settling ROADMAP §7.3).** Secrets are
  `EncryptedString` (AES-256-GCM under a key the server holds).

  Rationale is a hard constraint, not a preference: **Intuit rotates the QBO
  refresh token on every grant** (`qbo_client.py:177`), server-side, with no
  browser in the loop. The server must *write the new token back*. ADR-004's
  blind vault is defined by the server having no read path and no key
  material, and its `Opener` seam opens envelopes — it cannot seal a new one
  without the recipient key. A blind-at-rest regime therefore cannot hold a
  rotating credential at all. Applying it only to the static-key integrations
  would split one feature across two crypto regimes, which ADR-005's own
  "consequences" section already flags as a contributor hazard.

  Rejected: **an external secret manager** — adds a processor, egress on every
  provider call, and a second subsystem to make multi-tenant, for a blast
  radius the RLS wall plus field encryption already bounds. ADR-004's own
  "third-party PII vault" rejection applies unchanged.

- **D-OH17.3 — App-level credentials stay in `Settings`; only tenant-level
  ones move (CONFIRMED 2026-08-30).** The split is by *ownership*, not by
  integration:

  | Integration | Auth shape | App-level (stays) | Tenant-level (moves) |
  |---|---|---|---|
  | QBO | OAuth2 refresh grant, rotating | `qbo_base_url`, `qbo_client_id`, `qbo_client_secret` | `realm_id`, `refresh_token` |
  | Gusto | static bearer | `gusto_base_url` | `api_token`, `company_id` |
  | ADP | OAuth2 client-credentials | `adp_base_url` | `client_id`, `client_secret` |
  | Delphi | subscription key | `delphi_base_url` | `subscription_key` |
  | Tripleseat | API key | `tripleseat_base_url` | `api_key` |

  `qbo_client_id` / `qbo_client_secret` identify **our Intuit application**,
  not a tenant — one registration per deployment is correct, and moving them
  per-tenant would require every hotel to register an Intuit app. Every
  `*_base_url` is deployment routing (the mock-vs-real switch) and stays put
  for the same reason `crm_feed` left them behind at L5.

- **D-OH17.4 — The tenant-level `Settings` fields survive as **org-1 seed
  defaults only** (CONFIRMED 2026-08-30).** They are not deleted, and they are
  not a runtime fallback. `ensure_default_org` inserts org 1's credential rows
  from env **on first insert only**, exactly the `crm_provider` bridge
  precedent already in the tree (`property_registry.py:109-119`). At runtime
  every adapter resolves from the row, never from env.

  Rationale: it keeps `usali gusto-mock`, the demo seed, and the existing test
  suite working unchanged, and the blast radius of deleting them outright is
  large for a slice whose point is elsewhere. Rejected: **a runtime env
  fallback when an org has no row** — this is precisely the mutant L5 killed
  (`crm_api.py:59-72` documents it by name); it silently hands every new
  tenant our credentials.

- **D-OH17.5 — Typed nullable columns with a per-provider CHECK (CONFIRMED
  2026-08-30).** One column per real credential field, secrets as
  `EncryptedString`, and a CHECK that pins exactly which columns must be
  non-null (and which must be null) for each `(integration, provider)` pair.

  This is the `org_settings.crm_provider` / `org_checklist_override.item_key`
  discipline: the DB refuses a malformed credential row **independently of the
  app import**. Adding a provider costs a migration, which is the intended
  friction — a provider is not a thing that should be addable without one.

  Rejected: **two anonymous encrypted slots plus an account ref**
  (`secret_primary` / `secret_secondary`) — compact and migration-free, but
  the column names carry no meaning without a per-provider field map in
  Python, which is a second registry the DB does not know about. That is the
  smell D-B4.8 already rejected on the frontend side. Rejected: **one
  encrypted JSON blob** — the DB can enforce nothing about its contents, and a
  renamed key fails at provider-call time rather than at write time (ADR-010).

- **D-OH17.6 — Adapters are built per request from the org's row (CONFIRMED
  2026-08-30).** `_shared(get_qbo_client)` and `_shared(get_payroll_provider)`
  are deleted, and `_shared_by_key(get_crm_feed)` with them (it becomes
  unused). `_shared` itself survives for the face engine, whose ONNX sessions
  are a genuine per-process resource.

  `_shared` existed for QBO because the rotated refresh token lived in client
  memory (`server.py:145-152`). D-OH17.7 moves that lineage into the database,
  which **removes the reason the memoizer existed**. Cost is a fresh
  `httpx.Client` per operator action; these are pushes, pay runs and demand
  pulls — low-frequency deliberate acts, never page loads. The checklist probe
  builds no adapter at all (D-OH17.8), so nothing puts a provider client on
  the SPA's critical path.

  Rejected: **keying `_shared_by_key` on `org_id`** — the cached adapter holds
  a credential snapshot, so disconnect/reconnect would need explicit cache
  invalidation; the cache grows unbounded with tenant count; and the rotation
  lineage stays in process memory, leaving the restart bug in place and now
  multiplied per tenant.

- **D-OH17.7 — QBO refresh-token rotation is persisted to the row; it is
  DURABLE, not cross-process serialized (CONFIRMED 2026-08-30; the row-lock
  half REVISED 2026-08-30 during Task 5).** `QboClient` gains a `TokenStore`
  port — `load() -> str` and `store(refresh_token) -> None` — and the
  DB-backed implementation (`integrations.DbTokenStore`) reads and writes the
  org's `refresh_token` in its own short transaction per call.

  This closes a latent bug the class docstring already names: *"Refresh-token
  rotation is persisted IN MEMORY ONLY … a real deployment would persist the
  rotated token in a secret store"* (`qbo_client.py:112-123`). Today a process
  restart loses the rotation and the next push `invalid_grant`s against a
  bootstrap token Intuit has already consumed. Durability is what fixes that,
  and durability is what this decision delivers.

  **What it does NOT deliver, and why.** This decision originally said the
  store would take `SELECT … FOR UPDATE` so the row lock serialized
  *processes* where the in-process `threading.Lock` could not. That was not
  deliverable as stated: the critical section is `load()` → outbound grant →
  `store()`, so a lock taken and released inside either method covers none of
  it. Holding it across the grant would mean a transaction and a row lock open
  across a network call, with **no release path on failure** — the port has no
  abort step and `QboClient._refresh` raises without ever calling `store()`.
  Grant failure is routine (a revoked or expired refresh token fails every
  attempt), so that shape would trade a rare protection for a common leaked
  connection and a locked row.

  This is a SCOPE judgement, not an impossibility. `TokenStore` could grow a
  `rotating()` context manager (or an `abort()`) and get its `try/finally`,
  and (a) would then work as originally written. The port shape was frozen
  the task before, and reopening it to buy protection against a month-end
  race did not earn its way in.

  So the standing guarantee is **durability and per-tenant scope, and nothing
  more**. It is tempting to add "and serialized in-process by `QboClient`'s
  instance lock" — do not: that lock is per-INSTANCE, so it serializes only
  callers sharing one client, and **D-OH17.6 above deletes the `_shared`
  memoizer that was the reason they did**. Once each operator action builds
  its own client, two simultaneous pushes in one worker fork the lineage
  exactly as two workers would. In every case both spend the same token, the
  loser's grant returns `invalid_grant` and its push fails visibly, and the
  winner's rotated token is in the row, so a retry succeeds — nothing is
  silently lost. These are month-end operator actions, not a request path, so
  the exposure is small and recoverable. If it ever stops being small, the fix
  is a lock taken and released around the **whole refresh by the caller** —
  the `rotating()` port change above, or a Postgres advisory lock — never a
  `FOR UPDATE` smuggled into `load()`.

- **D-OH17.8 — The probe is a presence check; honesty is enforced on the
  WRITE path (CONFIRMED 2026-08-30).** `_probe_payroll` / `_probe_accounting`
  / `_probe_demand_feed` each become "does this org have a row for this
  integration?" — cheap, safe on every sidebar render, and still derived
  (D-B4.1): it reads what is actually configured, not a stored status.

  Truthfulness comes from making a bad credential un-storable. Saving
  key-based credentials makes **one live provider call and refuses to persist
  on failure**; the QBO OAuth grant verifies inherently by completing. A
  typo'd API key is a 422, not a `done` over an integration that 502s.

  A credential revoked *later* is a different event, and it surfaces where it
  should: a loud 502/503 at pull/push time. It is deliberately **not** a
  checklist `error` — the checklist reports on setup, and re-deriving live
  provider health on every page load would put two-to-five outbound calls on
  the critical path of the whole SPA and paint the page red during any
  provider outage.

  `connected_at` / `connected_by` are recorded as facts about the write event.
  **No probe reads them** — a `verified_at` consulted as status would be the
  stored copy D-B4.1 forbids.

- **D-OH17.9 — One `/integrations` page, `org_admin` only (CONFIRMED
  2026-08-30).** All five integrations on one org-scoped page (no property
  picker, the `/setup` shape). The role gate mirrors the dismissal gate and
  for the same reason: connecting a tenant's accounting system is a standing
  commitment about the tenant, not a per-user preference.

  Rejected: **sections on the existing feature pages** (QboPage, PayRunsPage,
  SchedulePage) — scatters one concern across three pages with three role
  gates, and `SchedulePage.tsx:204` renders no demand UI at all while
  unconfigured, which is the D-B4.8 complaint it would have to fix anyway.
  Rejected: **inline connect controls on `/setup`** — makes the checklist a
  write surface for secrets and breaks the "`where` routes you somewhere that
  closes it" contract D-B4.8 just established.

- **D-OH17.10 — Real OAuth for QBO; credential forms for the other four
  (CONFIRMED 2026-08-30).** Intuit issues no pasteable long-lived refresh
  token to an end user, so a paste-form for QBO would advertise a surface a
  tenant cannot complete — an item that looks closeable and is not. The other
  four authenticate with a token or key the tenant already holds, which is
  what their adapters do today (`gusto_adapter.py:45`, `adp_adapter.py:58`,
  `delphi_adapter.py:73`, `tripleseat_adapter.py:68`).

- **D-OH17.11 — The OAuth `state` is signed and carries the org (CONFIRMED
  2026-08-30).** The Intuit callback arrives as a top-level browser
  navigation with **no bearer token and no active-org header**, so
  `require_active_org` cannot run on it. `state` is therefore the only carrier
  of "which tenant is this for", and it must be unforgeable or it is a
  cross-tenant credential-injection hole. It is an HMAC-signed, short-TTL
  value bound to `(org_id, subject)`.

  **Amended 2026-08-30 while planning:** an earlier draft also required
  `state` to be single-use, consumed against a server-side nonce store. That
  store is **not** built. Replay is already dead without it, because the other
  half of the callback — Intuit's `code` — is single-use at Intuit: a replayed
  `state` necessarily carries a spent `code`, and the token exchange refuses
  it. A nonce table would add a row, a migration and a reaper to re-block
  something already blocked. The HMAC key is HKDF-derived from
  `field_encryption_key` under a fixed domain label — the `_photo_key`
  precedent (`crypto.py:79-113`) — so no new deployment secret appears.

  **Residual risk ACCEPTED 2026-08-30 (user decision).** The amendment's
  reasoning covers replaying a *whole* callback, and only that. It does not
  cover **state substitution with a fresh code**: an attacker holding a valid,
  unexpired `state` issued to org A's admin can run their own Intuit consent
  against their own QuickBooks company, then swap their `state` for the
  captured one. The MAC verifies, the callback derives org A, and the
  attacker's `code` is unspent — so the exchange succeeds and the attacker's
  `realm_id` and refresh token land on **org A's** accounting row, audited to
  org A's admin. Org A's subsequent QBO pushes then post into a book the
  attacker controls.

  Accepted rather than fixed, for three reasons in order of weight:

  1. The precondition is a captured `state` inside its 10-minute TTL. It is
     never sent to a third-party origin by us; it reaches an attacker only
     through the admin's browser history, our own access log, or a logging
     proxy — positions from which better attacks are usually already
     available.
  2. **A nonce store would not have closed it.** Single-use refuses only the
     *second* use, and nothing forces the legitimate callback to be first: an
     attacker who fires the substituted callback ahead of the admin consumes
     the nonce themselves, the write lands, and it is the admin's own connect
     that then fails. Single-use narrows the window to "before the admin
     finishes" and adds a loud symptom — worth something, but a narrowing,
     not a fix. This is the specific reason the earlier amendment is not
     simply reversed.
  3. The fix that would actually close it is binding `state` to the browser
     that began the flow: an opaque cookie set at `authorize` and required at
     `callback` (the standard OAuth CSRF pairing). That means putting a cookie
     requirement on the one route deliberately mounted outside every gate — a
     design change, not a hardening tweak. **If this is ever revisited, build
     that, not the nonce store.**

  Two properties make the attack detectable rather than silent, and both are
  load-bearing now that the hole is accepted: the connect writes an
  `integration_connected` audit event, and `realm_id` is stored in plaintext.
  A `/integrations` page that DISPLAYS the connected company id gives a tenant
  the one signal separating a hijack from a normal connection — carried into
  the frontend plan as a requirement, not a nicety.

- **D-OH17.12 — The B4 tripwire is deleted and replaced by its mirror image
  (CONFIRMED 2026-08-30).** `test_the_integration_items_have_no_connect_surface_yet`
  (`tests/test_checklist.py:215`) fails by design once a `where` is restored —
  that failure is the signal, not a regression. It is deleted **in the same
  edit** that restores the `where` values (two of the three — D-OH17.16) and
  removes `_OH17_REASON`,
  and replaced by `test_every_item_has_a_connect_surface`, which asserts no
  item carries a null `where`. The paired invariant D-B4.8 established
  (`where is None` ⟺ `unavailable_reason is not None`) stays pinned by the
  existing `test_where_and_unavailable_reason_are_paired`, which is now
  satisfied vacuously and is kept precisely so a future null-`where` item
  cannot slip in unexplained.

  **Amended 2026-08-30 by D-OH17.16:** "all three" turned out to be two.
  `demand_feed` keeps a null `where` and an honest `unavailable_reason`,
  because a credential does not finish that connection. The replacement test
  is narrowed to match, and the pairing invariant is no longer satisfied
  vacuously — it now has a real pair to check.

- **D-OH17.13 — Credentials are org-level, not property-level (CONFIRMED
  2026-08-30).** A hotel group with two QBO companies is a real shape and this
  design does not serve it. Org-level is a strict improvement on
  process-wide, matches where `OrgSettings` already sat, and the property
  dimension can be added later as a nullable `property_id` in the key without
  reshaping anything above. Building it now would double the surface for a
  tenant we do not have. YAGNI, deliberately — and recorded here so the next
  reader knows it was decided rather than missed.

- **D-OH17.14 — The `org_settings` row is dropped, not carried forward
  (CONFIRMED 2026-08-30).** The migration does not attempt to synthesize
  credential rows from the old `crm_provider` values, because the matching
  secret lives in env and a data migration that reads env is fragile.

  This is safe by enumeration, not by assumption: nothing writes `OrgSettings`
  except `ensure_default_org` for org 1 (`property_registry.py:117`), and
  ROADMAP §2.1 records that **no page in the SPA writes `crm_provider` at
  all**. The only row that can exist is org 1's, and D-OH17.4's seed
  reconstructs it from the same env on the next seed. The `l5a0orgsettings`
  downgrade already established this posture verbatim: *"the org_settings rows
  are pure config a re-seed reconstructs from env/operator input — not the I6
  carry-rows-through case."*

- **D-OH17.15 — The org-1 seed reproduces today's config exactly; it is a
  bridge, not a connect action (CONFIRMED 2026-08-30).** `ensure_default_org`
  writes org 1's rows straight from `Settings`, per integration, with the same
  meaning those defaults have today. It does **not** run the connect-time
  verification of D-OH17.8.

  The obvious-looking rule — "seed only when the env differs from the
  committed mock defaults" — is **wrong**, and the tree says so:
  `e2e_backend.py:399-401` reads *"Mock Gusto for the pay-run e2e. No env
  needed: the settings defaults already point the GustoAdapter at
  127.0.0.1:9300 with the static 'mock' token, and payroll_provider defaults
  to gusto."* An opt-in-on-non-default seed silently breaks `payrun.spec.ts`.
  The three integrations lean on their defaults differently:

  | Integration | Default | Seeded? |
  |---|---|---|
  | payroll | `payroll_provider="gusto"` + working mock token | Always — the e2e depends on it |
  | accounting | `qbo_refresh_token="mock"`, a placeholder | Always, but see below |
  | demand_feed | `crm_provider=""`, the OFF sentinel | Only when env sets a provider |

  The demand-feed row is gated on the existing `""` sentinel, so
  `demo.sh:91` / `deploy_app.sh:119` keep working and an unset env keeps
  producing the honest "skipped" note at `demo_seed.py:838`.

  Accepted asymmetry: a dev install seeds an `accounting` row holding the
  literal `"mock"` refresh token, which will fail on first push. That is
  **exactly today's behaviour** — not a regression — and `e2e_backend.py:393`
  already bootstraps a real token from the mock before the app starts. The
  alternative (verifying during seed) would put outbound provider calls inside
  `ensure_default_org`, which runs in most test worlds. Verification belongs
  on the tenant-facing write path, where a real tenant's credentials actually
  arrive; org 1 is the pilot/demo org and its rows are exactly as honest as
  the process-wide config they replace. A newly provisioned tenant gets no
  rows at all and correctly reads `open`.

- **D-OH17.16 — `demand_feed` keeps an `unavailable_reason` instead of a
  connect surface (CONFIRMED 2026-08-30, after execution).** Credentials
  alone do not connect a demand feed. Verification
  (`integrations.verify_credentials`) and every real pull (`crm_api:167`)
  need a property `crm_ref`, and the ONLY writer of `crm_ref` is the repo's
  YAML seed (`mapping/property_registry.py:327`, first-insert-only). No API
  sets it; `property_config_api` does not expose it. So no tenant can reach
  `done` through the product, and the refusal text, followed literally, tells
  an operator to edit a file in our source tree.

  D-OH17.12 gave all three integration items a `where` on the assumption that
  a credential closes them. For payroll and accounting that holds. For the
  demand feed it does not, and shipping the route anyway would have flipped
  the item to a link that cannot finish — the exact drift D-B4.1 and D8.3
  exist to prevent, and the same class of drift OH-17 was opened to fix.

  Two options were live: add a `crm_ref` field to `property_config_api`, or
  say so. **Say so**, because the field is the smaller half of that work: a
  tenant-settable `crm_ref` is a provider-specific identifier a tenant must
  look up in Delphi or Tripleseat, so it needs its own validation, its own
  refusal shape, and somewhere in the property-config UI to explain itself.
  That is a feature, not a field, and it is not this slice.

  So D-B4.8's pairing applies in its other direction: `where=None`,
  `unavailable_reason` set. Consequences worth naming:

  - The reason is worded to read correctly at **any** status, per `_status`'s
    standing note — org 1 IS connected, because we put its `crm_ref` in our
    own YAML. It describes the missing path, never the item's state.
  - `test_every_item_has_a_connect_surface` becomes
    `test_demand_feed_is_the_one_item_without_a_surface`, an EXACT set so it
    fails in both directions: any other item losing its route is a
    regression, and `demand_feed` gaining one is the signal that the reason
    must be deleted in the same edit. The same shape as the B4 tripwire this
    lineage started from.
  - `PUT /api/integrations/demand_feed` is unchanged and still works. The
    item is not un-connectable in principle — it is un-connectable
    *self-serve*, which is what the operator-facing string says.

## 3. Architecture

```
                  org-bound Session (both L2 walls)
                              │
                  ┌───────────┴───────────┐
                  │   integrations.py     │   ← the ONE resolution seam
                  │  resolve_payroll()    │
                  │  resolve_qbo()        │
                  │  resolve_crm_feed()   │
                  └───────────┬───────────┘
                              │ reads
                  org_integration_credential
                    (provider + secrets, one row)
                              │
        ┌─────────────────────┼─────────────────────┐
   payroll_run_api        portal_api (QBO)       crm_api
   checklist._probe_payroll / _probe_accounting / _probe_demand_feed
```

`integrations.py` is a new app-layer module owning credential resolution and
adapter construction. Its functions take the caller's **already-org-bound
session** — there is no `org_id` parameter to pass wrong, and both tenancy
walls confine every read automatically. Each returns an adapter or `None`
(not configured); `None` is what the call sites already know how to refuse
loudly.

It is its own module rather than more weight on `server.py` (past 600 lines of
factory wiring) for the reason `checklist_api.py` was split from
`portal_api.py`: the credential-resolution rules are a subject, not plumbing.

## 4. Data model

One new `OrgScoped` table. `org_id` is part of the composite primary key and
the FK to `organization` — the `OrgChecklistOverride` shape — so both L2 walls
confine it automatically.

```
org_integration_credential
  org_id            PK, FK organization.org_id
  integration       PK   'payroll' | 'accounting' | 'demand_feed'
  provider               'gusto' | 'adp' | 'qbo' | 'delphi' | 'tripleseat'

  realm_id          text            -- qbo      (identifier, not a secret)
  refresh_token     EncryptedString -- qbo      (ROTATES; see D-OH17.7)
  api_token         EncryptedString -- gusto
  company_id        text            -- gusto    (identifier, not a secret)
  client_id         text            -- adp      (identifier, not a secret)
  client_secret     EncryptedString -- adp
  subscription_key  EncryptedString -- delphi
  api_key           EncryptedString -- tripleseat

  connected_at      timestamptz  NOT NULL
  connected_by      text         NOT NULL   -- keycloak subject
```

`integration`'s legal set is the **schema mirror of the three integration keys
in `usali.checklist.ITEMS`**, kept literal on purpose, exactly as
`org_checklist_override.item_key` mirrors the full item set. `provider`'s set
is the schema mirror of `crm_feed.CRM_PROVIDERS` plus the payroll and
accounting providers.

The CHECK is a five-clause disjunction, one clause per `(integration,
provider)` pair, each naming the columns that must be non-null **and
requiring every other credential column to be null**. The "must be null" half
is what stops a stale `api_key` surviving a switch from Tripleseat to Delphi.

Identifiers (`realm_id`, `company_id`, `client_id`) stay plaintext
deliberately: they are not secrets, and being able to read them during a
support conversation is worth more than encrypting a company id.

**Dropped in the same migration:** `org_settings` (table, `org_wall` policy,
and `ck_org_settings_crm_provider`), per D-OH17.1 and D-OH17.14.

### The four hand-maintained lists

Adding an `OrgScoped` table touches four lists that do not update themselves.
All four change in the same commit, and two of them change **twice** here
because a table is also being removed:

1. `tests/test_l2_rls_wall.py::test_the_rls_inventory_is_complete_and_forced`
   — add `org_integration_credential`, remove `org_settings`. Miss the policy
   instead of the literal and the table has no wall at all.
2. `tests/test_models.py::test_tables_registered` — same two edits.
3. `tests/test_l4_org_grants.py::test_l4_is_the_single_alembic_head` — the
   head literal moves from `b2a0checklist` to the new revision.
4. `_L1_ORG_INDEPENDENT` in `tests/test_migration_on_populated_data.py` —
   **untouched**; this table is `OrgScoped` and must not go there.

Migration `b3a0integcred`, `down_revision = "b2a0checklist"`, copying
`l5a0orgsettings` as the template: `ENABLE` **and** `FORCE ROW LEVEL
SECURITY`, a policy named exactly `org_wall` with both `USING` and `WITH
CHECK` built from `usali.tenancy.RLS_ORG_VAR`, and **no GRANT** — the DEFAULT
PRIVILEGES `l2a0rlswall` recorded already cover future tables.

## 5. API

New router `integrations_api.py`, prefix `/api/integrations`, mounted with the
standard `operator_gates`, every route additionally gated by `org_admin`.

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/api/integrations` | All three integrations with their connection state |
| `PUT` | `/api/integrations/{integration}` | Connect/replace a key-based integration |
| `DELETE` | `/api/integrations/{integration}` | Disconnect (row delete) |
| `GET` | `/api/integrations/accounting/authorize` | Begin the QBO OAuth grant |
| `GET` | `/api/integrations/accounting/callback` | Complete it |

`GET` returns, per integration: `connected` (bool), `provider`, the
**non-secret identifiers only** (`realm_id`, `company_id`, `client_id`), and
`connected_at`. **No secret is ever returned** — the ADR-004 blind-read
posture applied to this store even though the server can technically decrypt.
Re-entering a key is how you change it.

`PUT` is the set-a-value shape, so the verb matches the `property_config_api`
convention and D-B4.5's reasoning. It validates the body against the target
integration's provider set, **makes one live provider call**, and persists
only on success (D-OH17.8). A failed verification is a 422 naming the
integration and the provider's status — never the response body, per the
existing adapter posture.

`DELETE` on an absent row is a 204 no-op, matching
`checklist_api.undismiss`.

Connect and disconnect both write an `AuditEvent`
(`integration_connected` / `integration_disconnected`, `resource_type =
"integration"`), matching `checklist_api._audit`.

### The OAuth pair

`authorize` builds the Intuit consent URL and returns it (the SPA navigates;
it does not 302, so the fetch seam and its `redirectToLogin` latch stay
untouched). `state` is HMAC-signed over `(org_id, subject, expiry)` per
D-OH17.11.

There is **no nonce**. An earlier draft of this section signed one and
consumed it server-side; D-OH17.11's 2026-08-30 amendment removed it, because
Intuit's `code` is already single-use at Intuit and a replayed `state`
therefore carries a spent code that the exchange refuses. This paragraph is
the one that has to say so, because "consume the nonce" read as a shipping
instruction long after the decision above had retired it.

That reasoning covers replaying a whole callback and nothing more. A captured
`state` paired with the attacker's OWN fresh code still binds their QuickBooks
company onto the victim org's row, and single-use would not have stopped it
either — see D-OH17.11's accepted-residual block, which is the canonical
statement of both the hole and why a nonce store is the wrong answer to it.

`callback` is mounted **outside** `operator_gates` — it arrives as a top-level
browser navigation with no bearer token, so `require_operator` and
`require_active_org` would both refuse it. It therefore does its own
authorization entirely from `state`: verify the signature, check the TTL, and
bind an org-scoped session from the org id inside it. This is the one route in
the system whose tenant identity comes from a signed parameter rather than a
validated token, and that is exactly why the signature and the TTL are
load-bearing rather than defence in depth.

## 6. Frontend

**The PAGE is DEFERRED to its own plan — amended 2026-08-30 during execution.
This slice is backend-only.** None of it shipped: there is no
`IntegrationsPage.tsx`, no `api/integrations.ts`, no nav entry, and no
`IntegrationsPage.test.tsx` (§8's frontend bullet goes with them). What did
ship from this section is "The checklist edit" below — the three probes, the
two `where="/integrations"` values plus `demand_feed`'s own
`unavailable_reason` (D-OH17.16), and the deleted `_OH17_REASON`. The rest
is kept as the specification the frontend plan starts from, not as a record of
what landed.

This is the largest scope change the slice took, so it is spelled out rather
than left to be inferred from the header's "scope FINAL": OH-17 delivers
per-tenant storage, resolution, the `/api/integrations` router and the QBO
OAuth pair, and the three checklist items point at `/integrations` — a route
**the SPA does not serve**, so an operator who clicks one reaches the
not-found page today. Two consequences are load-bearing and are handled where
they land: `.github/roadmap.yml` gets `in-progress`, not `shipped` (§10's
amendment), and `router.tsx`'s entry-route restore had to learn to reject a
remembered href that resolves to no route, or one such click would pin
Not Found onto every later bare-origin load (`lib/lastRoute.ts`).

The design below is unchanged and still the plan:

New `IntegrationsPage.tsx` at `/integrations`, plus `api/integrations.ts` and
the `Integration` types mirroring the router's models. A nav entry in
`Layout.tsx`'s ungrouped section beside Setup — the same reasoning that put
Setup there applies: integrations belong to neither Accounting nor Employee
Management, and the entry gets `show: (me) => hasRole(me, 'org_admin')`
because the whole page is org-admin-gated.

Each of the three integrations renders as a card: current state, a provider
picker where there is a choice (payroll: Gusto/ADP; demand feed:
Delphi/Tripleseat), the fields that provider needs, and Connect / Disconnect.
QBO's card has no fields — one "Connect to QuickBooks" button that navigates
to the URL `authorize` returned.

On success the page calls `useInvalidateChecklist()` from
`lib/useChecklist.ts`, so `/setup`, the sidebar badge and the dashboard card
all move together — they already share one query key and must not be able to
disagree about whether payroll is connected.

### The checklist edit

In `checklist.py`, in one edit: the three probes gain real bodies, **two** of
the three items get `where="/integrations"` with `unavailable_reason` back at
its `None` default, and `_OH17_REASON` is deleted. `demand_feed` keeps the
null `where` and gets a reason of its own, per D-OH17.16 — a credential does
not finish that connection. (This paragraph said "the three items" until
2026-08-30; the seam it describes is unchanged, only the count.)
`ChecklistItem.where` and
`ItemStatus.where` **stay `str | None`** — D-B4.8's paired invariant is a
permanent property of the registry, not scaffolding for this one gap, and the
next un-connectable item will need it again.

`ChecklistPage.tsx` needs **no change**: its `item.where !== null` branch and
its `unavailable_reason` paragraph are already written to handle both states.
That is the payoff for D-B4.8 having refused a frontend-side set of
"not connectable yet" keys.

## 7. Error handling

Per ADR-010, every degradation is loud and named.

- **No credential row** → the resolver returns `None`; call sites refuse with
  the existing loud posture. `crm_api`'s 503 text stops naming
  `USALI_CRM_PROVIDER` (now a lie — it is not the switch any more) and names
  the `/integrations` page instead. The QBO push and pay-run surfaces gain the
  same shape, which they have never needed before because a process-wide
  credential always existed.
- **Decryption failure** (a rotated `field_encryption_key` against existing
  ciphertext) → a 503 naming the integration, never a fallback to env and
  never a silent "not connected". ADR-005 already records that rotation makes
  existing ciphertext undecryptable; this is where a tenant would meet that.
- **Provider call fails during connect-time verification** → 422, integration
  and status code only. The payroll adapters' existing rule holds: on the
  PII-carrying sync path even the response detail is dropped.
- **Invalid, expired, replayed or missing OAuth `state`** → 400 with a fixed
  message, and no row written. It must not distinguish those cases: the
  difference is an oracle about other tenants' in-flight grants.
- **Concurrent QBO pushes** → NOT serialized, in one process or across them
  (D-OH17.7, revised). `QboClient`'s lock is per-INSTANCE, and D-OH17.6
  removes the `_shared` memoizer that made concurrent callers share one
  instance — so once each operator action builds its own client, two
  simultaneous pushes in a single worker fork the lineage exactly as two
  workers would. Both spend the same refresh token; the loser's grant returns
  `invalid_grant` and its push fails visibly, while the winner's rotated token
  is in the row, so a retry succeeds. ACCEPTED, not mitigated: these are
  month-end operator actions, and the alternative held a row lock across an
  outbound HTTP call with no release path on a failed grant.

## 8. Testing

- **Two-org isolation**: org A's credentials unreachable under org B's
  session, through the ORM wall and with the ORM wall bypassed (RLS alone) —
  the shape `test_l2_rls_wall.py` already uses.
- **Rotation durability**: push, discard the client, rebuild it from the DB,
  push again. This fails against today's code, which is the point — it is the
  regression test for the bug `qbo_client.py:112` documents.
- **Rotation under contention**: NOT a test, because it is no longer a claim
  (D-OH17.7, revised). Cross-process serialization was dropped, so "both
  succeed" is not guaranteed and a test asserting it would be asserting a
  behaviour the code does not promise. What IS tested is the guarantee that
  replaced it: a second `DbTokenStore` over the same org — the stand-in for a
  restarted process or a second worker — reads the ROTATED token, not the
  bootstrap one.
- **Connect-time verification refuses**: a bad key returns 422 and writes **no
  row**, so the checklist item stays `open` rather than going `done` over a
  broken integration. This is the D-OH17.8 assertion and the one most worth
  writing first.
- **The CHECK refuses malformed rows** at the DB, with the app import out of
  the picture: a `gusto` row carrying an `api_key`, a row with no secret at
  all, and a `demand_feed` row naming `qbo`.
- **OAuth state**: forged signature, expired TTL, and replay each refused; a
  valid state writes the row under the org named inside it and no other.
- **Checklist**: each of the three items reads `done` with a row and `open`
  without one; `test_demand_feed_is_the_one_item_without_a_surface` replaces
  the deleted tripwire (named `test_every_item_has_a_connect_surface` until
  D-OH17.16 narrowed it to an exact set).
- **The seed bridge** (D-OH17.15): `ensure_default_org` populates org 1 from
  env on first insert and does **not** overwrite an operator-set row on
  re-seed — the find-or-create posture `property_registry.py` already pins for
  `crm_ref`. Specifically pinned: a payroll row exists under the bare
  defaults (the `payrun.spec.ts` dependency), and **no** demand-feed row
  exists when `USALI_CRM_PROVIDER` is unset.
- **The e2e paths still pass unchanged** — `payrun.spec.ts` against the
  default-configured Gusto mock, and the `/qbo` push after
  `e2e_backend.py` bootstraps its refresh token. These are the two places a
  seeding-rule mistake would surface, and neither should need an edit.
- **Frontend**: `IntegrationsPage.test.tsx` per the existing page-test
  convention. Note the jsdom accessible-name trap — use `aria-label` on the
  per-integration buttons rather than an `sr-only` span, since a page of three
  cards repeats "Connect" three times.

  **Deferred with the page — amended 2026-08-30 during execution** (§6). The
  one frontend test this slice DID need is the entry-route consequence of
  shipping the `/integrations` links without the page:
  `lib/lastRoute.test.ts` pins that a remembered href with no route falls back
  to the dashboard instead of restoring Not Found forever.

## 8a. Raised and RESOLVED in execution — `pay_run.provider_name`

**Resolved 2026-08-30 in `986b5da`.** An earlier revision of this section
recorded the divergence as deliberately carried forward. That was reversed once
the severity was understood, and this section is rewritten rather than left
describing a hazard that no longer exists.

**What it was.** `payroll_run_api.create_run` recorded
`provider_name=settings.payroll_provider` (env) while the adapter beside it
resolved from the tenant's row. The first assessment called this a
record-keeping lie deferred on data-model grounds, since `provider_name` is
also the identity key of `ProviderEmployeeRef`.

**Why that was wrong, on both counts.**

*It was a mis-pay, not a mislabel.* `provider_name` is the ref lookup filter
(`payroll_run.py:1424`), and those refs become
`PayRunEntry(provider_employee_id=…)` at `:1562`, submitted at `:1569`. A tenant
on ADP while env said gusto would key refs `"gusto"` holding **ADP-side employee
ids**; a later switch to Gusto finds them fresh and submits ADP ids to Gusto.

*The re-keying fear was inverted.* `ProviderEmployeeRef`'s own docstring
(`models.py:1564`) says it is per-provider "so switching providers re-syncs
rather than clobbering the old mapping", and its
`UniqueConstraint("employee_id", "provider")` exists for that. Nothing existing
is orphaned either: the seed is insert-on-first-only, so every ref that exists
was created when the row and env agreed. Reading env was already the *less*
stable option — an operator changing `USALI_PAYROLL_PROVIDER` on a running
deploy silently orphans every ref today.

**The fix.** `resolve_payroll` returns `ResolvedPayroll(provider_name, adapter)`
rather than a bare adapter. The seam had been handing back an adapter with its
identity stripped off — the very thing D-OH17.1 forbids everywhere else. Pinned
by a test asserting both `pay_run.provider` and the `ProviderEmployeeRef.provider`
values come from the row when it disagrees with env.

**Still genuinely out of scope:** `cli.py:549` keeps its own
`_qbo_client_from_settings` reading `USALI_QBO_*`. The CLI is not org-aware at
all, so it diverges from the API's per-tenant resolution — acceptable while it
is an operator tool run against one deployment, but it should not grow a second
user.

## 9. Out of scope

- **Per-property credentials** (D-OH17.13).
- **Billing and the trial clock** — still OH-19, still D-B4.6.
- **SMS / the notification vendor** — ROADMAP §1.3, unrelated.
- **Ingestion-boundary redaction** — ROADMAP §2.4, item 3 in §6 sequencing,
  deliberately a separate slice.
- **A credential-rotation UI** beyond disconnect/reconnect, and key-rotation
  machinery for `field_encryption_key` (ADR-005 records the absence of
  envelope/versioning as a known gap; OH-17 does not close it).
- **Backfilling existing `org_settings` rows** (D-OH17.14).

## 10. Roadmap deltas this slice applies

On merge, update in the same commit:

- `docs/ROADMAP.md` §2.1 — OH-17 moves from the open lift to shipped, and the
  three "waiting connect surfaces" paragraph plus the tripwire paragraph go
  with it. §6's sequencing table row 2, and §7 open decision 3 (settled by
  D-OH17.2).
- `.github/roadmap.yml` — **OH-17 `planned` → `shipped`.** §8 records that
  nothing enforces the two files agreeing, and that OH-18 already drifted for
  two commits by exactly this omission. A status edit in the doc alone is half
  an edit.

  **Amended 2026-08-30 on merge:** the status this slice actually applies is
  `planned` → **`in-progress`**, not `shipped`, because §6's frontend was
  deferred (see the amendment there). The yml says so with the reason inline —
  storage, resolution and the OAuth backend shipped; no `/integrations` page
  exists, so no hotel can connect from the app. `shipped` belongs to the
  frontend plan that closes it. The paired-edit rule this bullet exists to
  enforce is unchanged: whatever the status is, both files say it in the same
  commit.
