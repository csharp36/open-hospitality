# OH-16 marketing deploy — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the `marketing/` site to Cloudflare Pages at `oh.mandati.ai`,
deployed from CI, with cookieless analytics.

**Architecture:** A `workflow_dispatch` workflow that builds `marketing/` and
publishes with `wrangler`, mirroring the maintainer-gated shape of
`deploy-demo.yml`. The API token lives only as a repo secret — never on a
laptop, never in a transcript.

**Tech Stack:** Cloudflare Pages, `wrangler` 4.x, GitHub Actions.

**Prerequisite:** `2026-09-01-oh16-marketing-site.md` complete — the site must
build before it can be deployed.

---

## Task 0: The human step (blocks everything below)

`gh` is already authenticated as `csharp36`, so the secrets can be set from
here. Nothing else in this plan can start until these exist.

**This task is done by the user, not by an agent.**

- [ ] **Step 1: Mint a scoped API token**

Cloudflare dashboard → My Profile → API Tokens → Create Token → Custom token.

Permission: **Account → Cloudflare Pages → Edit**. Nothing else. Do not use a
Global API Key — it can do everything to every zone, and this workflow needs to
publish static files.

Leave DNS permissions off: `oh.mandati.ai` is attached through the dashboard in
Task 3, which needs no token scope.

- [ ] **Step 2: Copy the Account ID**

Cloudflare dashboard sidebar, or the URL: `dash.cloudflare.com/<account-id>`.

- [ ] **Step 3: Store both as repo secrets**

Run these in the session with the `!` prefix so the values are prompted for
rather than echoed into the transcript:

```
! gh secret set CLOUDFLARE_API_TOKEN
! gh secret set CLOUDFLARE_ACCOUNT_ID
```

- [ ] **Step 4: Confirm they landed**

Run: `gh secret list`
Expected: both names appear. Values are never displayed — that is the point.

---

## Task 1: Create the Pages project

**Files:** none — this creates remote state.

- [ ] **Step 1: Create the project**

The project is created once, and the workflow deploys into it thereafter.
Run with the token exported for this command only, so it is not persisted:

```bash
CLOUDFLARE_API_TOKEN=<token> CLOUDFLARE_ACCOUNT_ID=<account-id> \
  npx wrangler pages project create open-hospitality-marketing \
  --production-branch main
```

Expected: confirmation that the project was created, and a
`*.pages.dev` URL.

If the user prefers not to paste the token even transiently, they can create the
project in the dashboard instead: Workers & Pages → Create → Pages → Connect to
Git is *not* what we want — choose **Direct Upload**, name it
`open-hospitality-marketing`. The workflow uploads; Cloudflare does not need
repo access.

- [ ] **Step 2: Record the pages.dev URL**

Note it. Task 2 verifies against it before the custom domain exists.

---

## Task 2: The deploy workflow

Mirrors `deploy-demo.yml`: `workflow_dispatch` only, a named environment, a
concurrency group, and comments that explain the choices rather than the syntax.

**Files:**
- Create: `.github/workflows/deploy-marketing.yml`

- [ ] **Step 1: Read the workflow it mirrors**

Run: `sed -n '1,60p' .github/workflows/deploy-demo.yml`

Match its action versions (`actions/checkout@v7` at time of writing), its
comment density, and its trigger shape.

- [ ] **Step 2: Write the workflow**

```yaml
name: Deploy marketing site

# Publishes marketing/ to Cloudflare Pages. Separate from deploy-demo.yml
# because the two have nothing in common but a repo: this ships static files to
# a CDN and cannot affect the running app, its database, or anyone's login.
#
# Trigger: workflow_dispatch only, matching deploy-demo.yml. Fork pull requests
# cannot run workflow_dispatch, so no outside contributor can reach the
# Cloudflare credential.
#
# The API token is scoped to Account -> Cloudflare Pages -> Edit and lives only
# as a repo secret. See docs/design/2026-09-01-oh16-marketing-front-door-design.md §11.

on:
  workflow_dispatch:

# A newer publish waits rather than racing an in-flight upload onto the same
# project.
concurrency:
  group: deploy-marketing
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment:
      name: marketing
      url: https://oh.mandati.ai
    timeout-minutes: 15
    defaults:
      run:
        working-directory: marketing
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: npm
          cache-dependency-path: marketing/package-lock.json

      - run: npm ci

      # SITE_URL drives canonical tags and the sitemap, so it must be the
      # canonical host rather than the *.pages.dev the upload returns —
      # otherwise every canonical points at the wrong origin.
      - name: Build
        env:
          SITE_URL: https://oh.mandati.ai
          APP_ORIGIN: https://demo.mandati.ai
        run: npm run build

      # The roadmap guard runs inside the build above; these are the metadata
      # and link assertions against dist/.
      - run: npm test

      - name: Publish to Cloudflare Pages
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
        run: npx wrangler pages deploy dist --project-name open-hospitality-marketing
```

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/deploy-marketing.yml
git commit -m "ci(oh16): deploy the marketing site to Cloudflare Pages

workflow_dispatch only, mirroring deploy-demo.yml. Ships static files to a
CDN; it cannot touch the app, its database, or anyone's session."
git push
```

- [ ] **Step 4: Run it**

Run: `gh workflow run "Deploy marketing site"`
Then: `gh run watch`
Expected: green, ending with a `*.pages.dev` deployment URL.

- [ ] **Step 5: Verify the deployed site**

```bash
curl -sI https://<project>.pages.dev/ | head -1
curl -s https://<project>.pages.dev/ | grep -o "<title>[^<]*</title>"
curl -sI https://<project>.pages.dev/pricing | head -1
curl -s https://<project>.pages.dev/robots.txt
```

Expected: `200` for both pages, the homepage title, and the robots file.

---

## Task 3: Attach `oh.mandati.ai`

`mandati.ai` is already a Cloudflare zone, so this is a record in a zone that
exists. No nameserver change — `auth.mandati.ai` and `demo.mandati.ai` are live
and are not touched.

- [ ] **Step 1: Add the custom domain**

Dashboard → Workers & Pages → `open-hospitality-marketing` → Custom domains →
Set up a custom domain → `oh.mandati.ai`.

Cloudflare writes the DNS record and provisions the certificate.

- [ ] **Step 2: Wait for the certificate, then verify**

```bash
curl -sI https://oh.mandati.ai/ | head -1
curl -s https://oh.mandati.ai/ | grep -o '<link rel="canonical" href="[^"]*"'
```

Expected: `200`, and a canonical of `https://oh.mandati.ai/`. A canonical
pointing at `pages.dev` means Task 2's `SITE_URL` did not apply — fix and
redeploy rather than leaving it.

- [ ] **Step 3: Verify the sitemap**

Run: `curl -s https://oh.mandati.ai/sitemap-index.xml`
Expected: XML referencing `sitemap-0.xml`, whose URLs are on `oh.mandati.ai`.

---

## Task 4: Analytics

**Files:**
- Modify: `marketing/src/layouts/Base.astro`

- [ ] **Step 1: Create the site in Cloudflare**

Dashboard → Analytics & Logs → Web Analytics → Add a site → `oh.mandati.ai`.
Copy the site token. It is not a secret: it ships in the page HTML.

- [ ] **Step 2: Add the beacon**

In `marketing/src/layouts/Base.astro`, immediately before `</body>`:

```astro
    <!-- Cookieless. No consent banner is required, which is why this vendor and
         not one that sets an identifier -- /your-data sells the product's data
         posture and must not need an asterisk. -->
    <script
      defer
      src="https://static.cloudflareinsights.com/beacon.min.js"
      data-cf-beacon='{"token": "REPLACE_WITH_SITE_TOKEN"}'
    ></script>
```

Substitute the real token for `REPLACE_WITH_SITE_TOKEN`.

- [ ] **Step 3: Confirm the site still has no other JavaScript**

Run: `cd marketing && npm run build && grep -c "<script" dist/index.html`
Expected: 2 — the JSON-LD block and the beacon. A higher count means something
pulled in client JavaScript; find it before shipping.

- [ ] **Step 4: Commit and redeploy**

```bash
git add marketing/src/layouts/Base.astro
git commit -m "feat(oh16): cookieless analytics on the marketing site"
git push
gh workflow run "Deploy marketing site"
```

- [ ] **Step 5: Verify it reports**

Load `https://oh.mandati.ai/` in a browser, then check Web Analytics in the
dashboard. Data can take a few minutes to appear.

---

## Task 5: Point the world at it

The site is live but nothing links to it.

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the link to the README**

Under the title, alongside the existing License and CI badges, add a line
linking to `https://oh.mandati.ai`. Match the surrounding style rather than
inventing a new badge row.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(oh16): link the marketing site from the README"
```

---

## Deferred: the canonical domain

The design records the intent of an `openhospitality.*` canonical with
`oh.mandati.ai` as a 301. That is deliberately not in this plan — it depends on
a domain purchase, and the host-agnostic build reduces it to:

1. add the zone to Cloudflare and attach the domain to the same Pages project;
2. change `SITE_URL` in `deploy-marketing.yml`;
3. add a bulk redirect from `oh.mandati.ai/*` to the new host, so only one
   hostname answers 200.

Do all three together. Two hosts both serving 200 splits whatever ranking the
site has earned, which is the failure the design calls out in §16.
