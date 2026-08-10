# ADR-004: HPKE client-side PII sealing — blind vault + Opener seam

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

Payroll requires storing highly sensitive **store-and-forward** secrets — SSN, bank
account/routing numbers, tax elections. The server has to *hold* them and later hand
them to a payroll provider, but it has no reason to *read* them in between. Holding a
readable copy at rest is a liability we can avoid.

## Decision

We will **seal these fields in the browser** and keep the server blind to them at rest.

- **HPKE** (`DHKEM(P-256) / HKDF-SHA256 / AES-256-GCM`) seals each field to a recipient
  key the serving path cannot read. The envelope (`version, suite, key_id, enc, ct`) is
  pure structure with **no read path and no key material** on the server.
- The vault API is **blind-overwrite**: writes accept opaque envelopes; reads return only
  "on file" / "not on file", never plaintext.
- **AAD binds each ciphertext to `employee_id:field`**, so an envelope cannot be
  replayed onto another employee or field.
- An **`Opener` seam** opens envelopes only where legitimately needed (at pay-run time):
  `SoftwareOpener` in dev, an `HsmOpener` drop-in for production; production **refuses**
  the in-process software opener.
- Browser↔server crypto agreement is pinned by a **committed cross-library interop
  fixture** so a library upgrade on either side can't silently diverge.

## Consequences

- A database dump yields **no readable PII** for these fields.
- There is **no read-back path** — a corrupt or wrong-key seal surfaces loudly at
  provider-send time, not as a silent bad value.
- Requires client-side crypto and recipient-key management.
- This regime covers only store-and-forward secrets; values the server must **compute
  on** use a different regime (see ADR-005).

## Alternatives considered

- **Server-side symmetric encryption for everything** — rejected for these fields: the
  server would hold the key, defeating the "blind at rest" goal.
- **A third-party PII vault** — rejected: adds a processor, egress, and another trust
  boundary for data we can keep sealed in our own store.
