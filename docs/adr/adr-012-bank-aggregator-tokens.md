# ADR-012: Bank data via aggregator tokens — the Pillar E5 amendment

- **Status:** Proposed
- **Date:** 2026-09-06
- **Deciders:** Open Hospitality maintainers

## Context

Pillar E5 (the employment-model design) replaced singular bank fields with
`deposit_account` rows whose account and routing numbers are HPKE-sealed
client-side per ADR-004: the server never holds a bank identifier it can
read. `build-vs-integrate.md` extended that posture into a product verdict —
"bank reconciliation: don't build" — partly *because* OH was designed to
never touch banking.

The 2026-09-06 roadmap revision reverses the product verdict: bank feeds and
reconciliation (OH-28) are now the slice that makes OH the books rather than
a report about them. That requires reading transactions from the hotel's
**business operating accounts** daily. The question is how to do that without
giving up what E5 actually protects.

The mechanism the market settled on is the bank-data aggregator (Plaid,
Yodlee, Finicity, MX): the account holder authenticates on the aggregator's
own consent screen — increasingly via the bank's OAuth — and the client
application receives an **access token** scoped to read transactions. The
application never sees credentials, and the token cannot move money.

## Decision

We will amend E5, not reverse it. Three properties are **kept**, unchanged:

- OH never stores an account number, routing number, or online-banking
  credential in a form the server can read.
- Employee direct-deposit details remain HPKE-sealed client-side (ADR-004).
- Money movement stays delegated — this ADR covers *read*, and nothing here
  may grow into payment initiation without a superseding ADR.

What changes:

1. **OH holds aggregator access tokens** granting read-only transaction and
   balance access to business operating accounts. Products beyond
   transactions/balances — anything that returns account numbers (e.g.
   Plaid's Auth product) or initiates payment — are not requested, so the
   token *cannot* return what E5 forbids storing.
2. **Tokens are per-tenant integration credentials**, stored exactly like
   the QBO refresh token: an `org_integration_credential` row per (org,
   bank connection), secrets in `EncryptedString` per ADR-005, connected and
   disconnected from the `/integrations` page. Disconnect is a row delete
   *plus* a revocation call to the aggregator (Plaid: item removal), so a
   deleted row does not leave a live grant behind.
3. **Every read is audited.** Each transaction pull writes an audit event
   naming the org, the connection, and the date range — the same discipline
   as the sealed-PII Opener path.
4. **Plaid is the first adapter, behind a port, per ADR-009.** The port
   ships with the Plaid adapter, a runnable mock the test suite exercises
   offline, and Yodlee as the named second shape (built when a real owner's
   bank requires it — historically wider small-bank coverage). Statement
   upload is a fallback for banks no aggregator reaches, never the primary
   path.

Two preconditions gate production use, recorded here so they are not
rediscovered: a **legal entity** to hold the aggregator agreement, and a
**coverage check** of the pilot properties' actual banks against the
aggregators' institution lists (flagged unverified in
`reference/competitive-positioning-2026-09.md`).

## Consequences

- OH can reconcile the books daily against real bank activity — the core of
  OH-28 — while the credential holder remains the aggregator's consent
  screen, and a stolen token yields read access to business transactions,
  not the ability to pay or the employee PII vault.
- "Read access to business transactions" is still a real breach class. The
  mitigations are the ones OH already practices — ADR-005 encryption at
  rest, RLS on the credential row, audit on every read, revocation from the
  integrations page — plus the product-scope limit in decision 1.
- A per-connection aggregator fee becomes part of hosted-service cost, and
  the aggregator becomes an availability dependency for reconciliation
  (mitigated by the port: a second adapter, and statement upload as the
  degraded mode).
- The privacy story gains a sentence it can state plainly: *your bank logs
  you in; we store a revocable, read-only token; we never see your
  credentials or account numbers.* That is only true while decision 1's
  product-scope limit holds — reviews of bank-related changes must check it.
- Self-hosted deployments need their own aggregator agreement to use bank
  feeds; the feature degrades to statement upload without one. This is a
  natural seam for the open-core boundary (ROADMAP §6.3) but is not decided
  here.

## Alternatives considered

- **Storing bank credentials and scraping directly** — rejected outright:
  reverses E5 rather than amending it, and puts OH in the credential-breach
  business the aggregators exist to end.
- **Statement upload as the primary path** — rejected: daily reconciliation
  is the product; monthly PDF uploads reproduce the workflow whose
  weaknesses in incumbent products this slice exists to beat. Kept as the
  fallback only.
- **HPKE-sealing the aggregator token (ADR-004 style)** — rejected: the
  server must use the token daily in scheduled pulls with no client present;
  a server-unreadable seal cannot work here. ADR-005 is the regime for
  compute-on secrets, and the QBO refresh token is the exact precedent.
- **Yodlee (or Finicity/MX) first** — rejected for v1: Plaid's low-volume
  pricing is public and usable from day one and its developer tooling is
  stronger; the port keeps the switch cheap if the coverage check says
  otherwise.
- **Waiting for open-banking APIs (FDX/1033) and integrating banks
  directly** — rejected: per-bank integrations are the aggregator's whole
  product; rebuilding that surface is the "interface burden" argument from
  the positioning analysis, one layer down.
