# OH-16 — the public marketing front door (design)

Status: **APPROVED — scope FINAL (2026-09-01).** Closes ROADMAP §1.1, the
first half of Band 1 (discover → try → sign up). Depends on the visual
skin resolved in [Track A §7](2026-08-16-track-a-front-door-preview-design.md)
and inherits the honesty posture of
[D8](2026-08-16-data-posture-progressive-onboarding-design.md).
Plan doc follows via the writing-plans skill.

## 1. Goal & north star

A hotel owner or management company that has never heard of us can find
this product, understand in one screen that it runs the money *and* the
labor for every property they operate, and reach the `/try` preview —
without knowing a URL by heart.

Today `/try` and `/signup` are routes inside the app SPA served from
`demo.mandati.ai` (`frontend/src/router.tsx:241`, `:254`). They are the
designed aha moment and nobody can find them. There is no positioning,
no pricing, and nothing a search engine can index.

**The message this site must land, which no surface currently makes:**
Open Hospitality is an operating platform, not a report parser. One
property or one hundred, financials and labor together, in one account.

## 2. Scope

**In scope:**

1. A **static marketing site** — `marketing/`, Astro, three pages: Home,
   Pricing, Your data.
2. **Discoverability** — per-page metadata, sitemap, `robots.txt`, and a
   `robots.txt` for the app host so the two do not compete.
3. A **forward band** rendered from `.github/roadmap.yml`, with a build
   guard (§7).
4. **Token extraction** — the brand skin becomes one file with two
   readers (§8).
5. **Deploy** to Cloudflare Pages from CI, host-agnostic (§11).
6. Two **copy corrections** in existing surfaces (§13).

**Out of scope, deliberately:** a blog or content collections (Astro's
structure permits them; no posts ship); email capture on the marketing
site (§5c settles why); pricing numbers (OH-19 owns them); moving the
app's `/` entry route; i18n; lifting the invite gate (ROADMAP §1.2 is a
separate item).

## 3. Decisions locked in the brainstorm

- **Audience is the hotel operator, singular in kind and plural in
  scale.** Owners and management companies, one property or a hundred.
  Not the open-source contributor — the license and the repo link are a
  trust signal in the footer, not a second track through the site.
- **Separate static site, separate host.** Not routes inside the SPA.
  Two reasons, and the second is the deciding one:
  - `demo.mandati.ai` is named *demo*. A marketing site cannot rank
    from it and cannot read as the product's home.
  - `/` in the app is not a page. It is an entry point that redirects to
    your last route (`frontend/src/router.tsx:77`), and `dist/index.html`
    is the SPA shell every history fallback serves. A marketing home page
    cannot take `/` without moving the app's entry route, which drags in
    `lastRoute`, the post-login bounce, the registered OIDC redirect URIs,
    and `DEMO_APP_HOST`. That is real risk to a working app for no
    marketing gain.
- **Host-agnostic build.** An `openhospitality.*` canonical is the
  intent, with `oh.mandati.ai` as a 301. Nothing in the code depends on
  which domain we land: internal links are relative and two env values
  carry the rest (§11). A registrar surprise costs a config change.
- **`mandati.ai` is already a Cloudflare zone**, so `oh.mandati.ai` is
  available on day one as a Pages custom domain — one record in a zone
  that already exists, with no nameserver migration and therefore no
  exposure for the live `auth.mandati.ai` and `demo.mandati.ai`. This
  makes the new domain a preference rather than a prerequisite: the site
  can ship on `oh.mandati.ai` and adopt an `openhospitality.*` canonical
  later, which the host-agnostic build (§11) reduces to a config change
  plus a 301.
- **Two registers on one page.** Positioning and framing may run ahead
  of the product; anything a visitor can immediately check may not.
  The reason is specific to this product: the CTA is `/try`, which runs
  the real parser in about ten seconds, so the distance between promise
  and product is tested inside a minute rather than months into a POC.
  The forward band (§7) is where running ahead is loud, and it carries a
  tense.
- **One CTA, no forms.** Everything drives to `{APP_ORIGIN}/try`. Email
  is asked for there, after the visitor has seen their own numbers, by
  the `RequestAccess` component that already exists. The marketing site
  therefore makes no cross-origin request and needs no CORS, no rate
  limiting, and no abuse guards.
- **Homepage is before → after, widened.** The transformation is the
  proof, not the pitch. It sits at §2 of the page under a hero that
  claims the platform.

## 4. Architecture overview

`marketing/` is its own package with its own build and its own deploy.
It never imports from `frontend/`, and the app never imports from it.
Exactly two couplings exist, both one-directional and both read-only.

```
   ┌──────────────────────── marketing/ (Astro, static output) ────────────────────────┐
   │  src/pages/index.astro · pricing.astro · your-data.astro                          │
   │  src/layouts/Base.astro  → <head> metadata, nav, footer                           │
   │  src/components/*.astro  → Hero, Transformation, Pillars, Portfolio, Forward      │
   │  src/lib/roadmap.ts      → reads ../.github/roadmap.yml at BUILD time  ───────┐   │
   │  src/styles/global.css   → @import ../../shared/brand.css  ──────────────┐    │   │
   └──────────────────────────────────────────────────────────────────────────┼────┼───┘
                     │ astro build → static HTML, zero client JS              │    │
                     ▼                                                        │    │
        Cloudflare Pages ──── CTA href={APP_ORIGIN}/try ───▶ demo.mandati.ai   │    │
                                                                              │    │
   ┌──────────────────────────── existing repo ────────────────────────────┐  │    │
   │  shared/brand.css        ◀── also imported by frontend/src/index.css ──┘    │   │
   │  .github/roadmap.yml     ◀── the canonical status catalogue ────────────────┘   │
   │  frontend/public/samples/opera-trial-balance-sample.pdf → the "before" asset    │
   └────────────────────────────────────────────────────────────────────────────────┘
```

## 5. Components & boundaries

### 5a. `marketing/` — the site

Astro with `output: 'static'` and the Tailwind v4 Vite plugin, matching
the frontend's Tailwind major. No client-side framework and no hydration:
every page is content, and nothing on it needs JavaScript.

`Base.astro` owns the `<head>`, the nav, and the footer, so metadata and
chrome are defined once. Each page supplies title, description, and OG
image; the layout supplies the rest.

### 5b. `src/lib/roadmap.ts` — the only build-time data source

Parses `.github/roadmap.yml` and exposes the featured entries to the
forward band. It is the site's single point of contact with product
state; nothing else in `marketing/` reads outside its own directory.

### 5c. What is deliberately absent

No API client, no form handler, no environment secret at runtime. The
built output is HTML, CSS, and images. This falls out of the one-CTA
decision, and it is worth naming as a property rather than an accident:
a static site with no inputs has no request surface to abuse.

## 6. The three pages

### Home

1. **Hero** — portfolio-forward. *"Run the money and the labor for every
   property you operate."* Eyebrow: *one property or one hundred*. The
   sub-headline says the platform runs operating statements, department
   labor cost, schedules, and time clocks, and that it starts with **data
   your PMS already produces** (§13 explains that phrasing). Primary CTA
   *See it on your own report*; secondary *Run a sample instead*.
2. **The transformation** — the before → after panel. An unreadable
   trial balance becomes a Summary Operating Statement. One glance, no
   explanation.
3. **What it runs** — four pillars, every one shipped today:
   - *Financials* — USALI operating statement, drill-through to the
     transaction row behind any line, QuickBooks and CPA pack export.
   - *Labor* — Schedule 14/15 cost and hours by department, target hours
     against rooms sold, overtime and productivity.
   - *Workforce* — employees, scheduling, an iPad time clock with
     server-enforced punch order, pay runs.
   - *Connections* — Opera, AutoClerk and SkyTouch in; your own
     accounting and payroll accounts connected out (OH-17).
4. **Built for more than one** — every property in one account, isolation
   enforced by the database rather than by application code that has to
   remember. The side-by-side roll-up shown here is marked *shipping
   next*, because OH-11 is `planned`.
5. **Shipping next** — the forward band (§7).
6. **Close** — the CTA again, with the trust line: nothing saved, no
   account needed, Apache-2.0 and self-hostable.

### Pricing

The philosophy page, and no numbers. OH-19 has not chosen a pricing
basis and there is no paying tenant to calibrate against, so a number
published here would be a guess wearing a commitment's clothes.

What it does answer, because these are decided: the open core is free
and self-hostable in perpetuity; the hosted product is what costs money;
we are in pilot and pricing lands at GA. That addresses the question
behind the question — *am I going to get squeezed once I depend on this?*

### Your data

The objection most likely to stop an upload, from a visitor being asked
to hand their P&L to a site they have not heard of. Every claim on this
page already exists as a property of the system:

- the `/try` preview persists nothing;
- tenant isolation is row-level security at the database, not a code
  convention;
- SSN, bank, and tax elections are sealed client-side and the server
  never holds them in plaintext at rest;
- Apache-2.0, self-hostable, so the exit is real.

## 7. The forward band, and the build that polices it

`§5 Shipping next` renders from `.github/roadmap.yml`. The copy for each
entry is written for the site; the **status** comes from the file.

The guard is the point. **The build fails when a featured id is missing
from the catalogue, or when its status has become `shipped`.** Marketing
copy about future work rots in one direction — a feature ships and the
site goes on calling it upcoming — and this turns that specific rot into
a red build. A shipped feature must be promoted into the page proper
before CI is green again.

Featured at ship: **OH-11** portfolio roll-up (`planned`), **OH-15**
operational KPI alerts (`considering`), **OH-8** budget import and
variance (`planned`), and **OH-22** direct PMS integrations, opened by
this work.

**OH-22 is new.** Direct PMS integration appears nowhere in the product
or the catalogue today — ingestion is file-based (`POST /ingest` takes an
`UploadFile`), and OH-2 is scoped to *report/exports … new file shapes*,
not connectors. Since the band can only promise what the file contains,
the entry is opened as part of this work.

## 8. Design tokens: one file, two readers

The `--color-brand-*` tokens and `--font-display` currently sit inside
`frontend/src/index.css` under a "Marketing front door" comment. They move
to `shared/brand.css` at the repo root, imported by both
`frontend/src/index.css` and `marketing/src/styles/global.css`.

Not a copy and not a synchronization test. One file with two readers —
`/try` and the marketing site cannot drift apart, because there is no
second definition to drift from.

The app's own tokens (`--color-surface`, `--color-accent`, the
categorical series and its validation record) stay exactly where they
are. Only the marketing block moves.

## 9. The "before" asset

The transformation panel needs an input that reads as genuinely messy.
The real exports under `~/Desktop/Sample Hotel` carry production figures
and cannot be committed or published.

They are not needed. `scripts/gen_preview_samples.py` already generates
the synthetic Opera trial balance committed at
`frontend/public/samples/opera-trial-balance-sample.pdf`, which is what
`/try` runs its own sample against; `tests/test_preview_samples.py` is
where that sample is pinned to keep parsing. A build script renders its
first page to a WebP for the panel. The image on the marketing site is then the same
synthetic report the preview itself demonstrates on — nothing new to
redact, and no path by which a real export reaches the repo.

## 10. Indexability

Per page: `<title>`, meta description, canonical, Open Graph and Twitter
card tags, and JSON-LD. `@astrojs/sitemap` generates `sitemap.xml`;
`robots.txt` ships in `public/` and references it.

**The app host needs one too.** `demo.mandati.ai` currently serves no
`robots.txt`, so the whole SPA is crawlable and will compete with the
marketing site on the same terms. A `robots.txt` in `frontend/public/`
disallows the app routes and allows `/try`, which is the one app URL that
should rank.

Only one hostname may answer 200 for this content. The other 301s to it.
Two live copies split whatever ranking the site earns.

## 11. Deploy

Cloudflare Pages, driven from CI by a new
`.github/workflows/deploy-marketing.yml` that builds `marketing/` and
publishes with `wrangler`. Chosen over a second Cloud Run service or a
GCS bucket behind a load balancer for three reasons: certificates and
custom domains are handled, the CDN is global and free at this volume,
and its CNAME flattening resolves the apex problem — a CNAME is illegal
at a zone apex, so an apex canonical otherwise requires ALIAS support.

**DNS is already on Cloudflare**, `mandati.ai` included, so the platform
decision costs no migration and the first custom domain
(`oh.mandati.ai`) is a record in a zone that already exists.

Two configuration values, both env with defaults:

| Value | Default | Drives |
|---|---|---|
| `APP_ORIGIN` | `https://demo.mandati.ai` | every CTA href |
| `SITE_URL` | the Pages preview URL | canonical tags, sitemap |

Two repo secrets, `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.

**One human step gates the rest**, and only one: minting the first API
token, scoped to Account → Cloudflare Pages → Edit rather than a global
key. A token cannot be issued without a browser session. Everything
after it — creating the Pages project, setting the repo secrets,
attaching `oh.mandati.ai` as a custom domain, running the deploy and
verifying it responds — runs from here.

## 12. Analytics and data posture

Cloudflare Web Analytics: cookieless, no consent banner required, no
additional vendor, and already part of the platform being deployed to.
Enough to see which pages send visitors to `/try`.

This is chosen so the *Your data* page needs no asterisk. A tracker that
required a cookie banner would contradict the page selling the product's
data posture.

## 13. Changes outside `marketing/`

1. `shared/brand.css` extracted, and `frontend/src/index.css` imports it
   (§8).
2. `robots.txt` added to `frontend/public/` (§10).
3. **"the report your PMS already emails you" retired.** The phrasing is
   in the `/try` page copy and reproduced across the README and the
   Track A doc. It hard-codes today's transport into the product's
   self-description at the moment OH-22 opens a second one. The
   replacement is **"data your PMS already produces"** — true whether it
   arrives as a nightly export, an email, or an API call. The marketing
   site and the `/try` page both adopt it, so the two surfaces a visitor
   sees in sequence do not contradict each other.
4. `.github/roadmap.yml` gains **OH-22** (§7).
5. `docs/ROADMAP.md:411` still lists OH-17 as `planned` while
   `roadmap.yml` says `shipped`. Unrelated to this work, noticed during
   it, and the same drift that document already records for OH-18.
   Corrected here because §7's guard reads the catalogue and the two
   files disagreeing is exactly what it exists to prevent.

## 14. Testing

Weighted toward build-time assertions. A static site's failure modes are
broken links, missing metadata, and stale claims — none of which a DOM
test is the natural instrument for.

- every internal link resolves to a file in the build output;
- every page carries title, description, canonical, and an OG image;
- the §7 guard: every featured roadmap id exists, and none is `shipped`;
- one Playwright smoke test: the primary CTA resolves to
  `{APP_ORIGIN}/try`.

## 15. Dependencies and open items

- **Domain registration blocks nothing.** `openhospitality.com` is
  likely taken and the alternatives cost money and a decision, but
  `oh.mandati.ai` is available immediately in an existing zone. The
  preferred canonical is an upgrade applied later, not a prerequisite.
- **The first API token** — the one human step in §11.
- **OH-19** is not blocked by this and does not block it. When pricing is
  decided, the Pricing page gains numbers; nothing else changes.

## 16. Risks

- **The forward band over-promises.** Mitigated by the tense marker and
  by §7's guard, but the guard checks status, not tone. A reviewer should
  read §5 of the page as a stranger would.
- **The two hostnames both go live.** Splits ranking and creates
  duplicate content. The 301 is part of the deploy, not a follow-up.
- **Token extraction touches a working stylesheet.** `frontend/src/index.css`
  is imported by the running app; a bad extraction is a visible
  regression on every page. The move is mechanical and the frontend test
  suite runs against it.
