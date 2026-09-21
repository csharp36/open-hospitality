# `oh-email-intake` — the Cloudflare Email Worker

Ingress for OH-23, emailed night audits. Mail sent to any address at
`intake.mandati.ai` reaches this worker through an Email Routing catch-all
rule; the worker signs the raw message and POSTs it to
`POST /api/intake/email`, and the app decides what it is and what to do with
it.

Design: [`../../docs/design/2026-09-21-oh23-emailed-intake-design.md`](../../docs/design/2026-09-21-oh23-emailed-intake-design.md)
(D-OH23.1 is this worker). Setup, the first time and after a rotation:
[`../../docs/runbooks/email-intake.md`](../../docs/runbooks/email-intake.md).

## What it does, and what it deliberately does not

It reads `message.raw`, and if the running total passes `MAX_BYTES` it stops
reading and forwards the message to the fallback mailbox. Otherwise it POSTs
the bytes as `message/rfc822` with five headers:

| header | value |
| --- | --- |
| `X-Intake-Timestamp` | unix seconds, decimal |
| `X-Intake-Signature` | `sha256=` + HMAC-SHA256 over `timestamp + "\n" + body` |
| `X-Intake-To` | `message.to` — the **envelope** recipient |
| `X-Intake-From` | `message.from` — the **envelope** sender |
| `X-Intake-Auth` | the message's `Authentication-Results` header, or `""` |

It does not parse MIME, does not look at attachments, does not log message
content, and never calls `setReject`. A bounce to a PMS's automated sender is
silent, so the only two dispositions are "the app took it" and "the fallback
mailbox has it".

**A non-2xx means forward.** `../../src/usali/intake_api.py` is where the
status codes are chosen, and it reserves non-2xx for "this service did not
dispose of the message". Everything the app *did* decide — an unknown address,
a rejected sender, an attachment that would not parse — comes back 200 with an
outcome, and the worker just logs that JSON.

**The fallback mailbox holds unredacted mail.** It sits outside the app's
redaction gate, which is why the runbook calls it an ops mailbox and says so
again there.

## Configuration

`wrangler.toml` `[vars]` carries `INTAKE_URL` and `MAX_BYTES`. The two secrets
are set by the deploy workflow, never committed:

| worker secret | from GitHub secret |
| --- | --- |
| `INTAKE_SECRET` | `EMAIL_INTAKE_SECRET` |
| `FALLBACK_ADDRESS` | `EMAIL_INTAKE_FALLBACK` |

`INTAKE_SECRET` is one value in two places — here, and
`USALI_EMAIL_INTAKE_SECRET` in GCP Secret Manager, which
`scripts/cloud/deploy_app.sh` mounts into the app. They rotate together or
every message 401s and lands in the fallback mailbox.

## Tests

```sh
npm ci && npm test      # node --test, no wrangler and no network
```

`test/sign.test.js` asserts the same fixed vector the app does
(`../../tests/test_intake.py::test_the_signature_vector_verifies`) — two
implementations of one construction are only the same construction while both
agree on a fixed answer. `test/worker.test.js` drives the `email` handler with
a fake `message` and a stubbed `globalThis.fetch`.

CI runs both in the `email-intake` job of `../../.github/workflows/ci.yml`.

## Why `wrangler` is pinned here and not in `marketing/`

`marketing/package.json` has no `wrangler` dependency at all; its workflow
calls `npx wrangler pages deploy`, which resolves whatever is current on the
day it runs. That is tolerable there: the command uploads static files to a
Pages project, and a wrangler that misbehaves fails the upload visibly.

This package pins an exact version (`4.136.1`) because `wrangler deploy`
*compiles and uploads code* and `wrangler secret put` *writes a credential*,
and because this deploy is dispatched by hand months apart — the run that
matters is the one after a long gap, which is exactly when an unpinned major
would have moved underneath it. Dependabot bumps the pin like any other
dependency, in a pull request, rather than at deploy time.

## Deploying

`.github/workflows/deploy-email-intake.yml`, `workflow_dispatch` only. It
needs `CLOUDFLARE_WORKERS_API_TOKEN` — a **different** token from the
Pages-scoped `CLOUDFLARE_API_TOKEN` the marketing deploy uses. See the
workflow's header comment for the scopes.
