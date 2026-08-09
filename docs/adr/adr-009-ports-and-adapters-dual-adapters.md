# ADR-009: Ports-and-adapters with dual adapters + a mock per integration

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

The engine integrates several external systems — a payroll provider, a CRM demand feed,
QuickBooks Online, an object store for photos, and the PII `Opener`. A port defined
against a **single** vendor tends to quietly encode that vendor's assumptions, so the
"abstraction" leaks the moment a second real integration arrives.

## Decision

We will use **ports-and-adapters**, and for each integration **prove the port** by
implementing **two deliberately different-shaped adapters plus a runnable mock**.

- The **payroll provider** set the pattern (two real shapes flushed out assumptions the
  interface had baked in); it was then applied to the **CRM feed**, **QBO client**, the
  **photo store**, and the **Opener** seam.
- Each integration ships a **mock** the test suite runs against offline, so no external
  credentials or network are needed to exercise the port.

## Consequences

- The port stays **honest** — a second concrete shape is what reveals a leaked
  assumption; one adapter cannot.
- Every integration is **testable offline** against its mock.
- Slightly more adapter code up front, and a discipline to maintain: a new integration
  is expected to arrive with a mock and a second-shape story, not a single adapter.

## Alternatives considered

- **One adapter behind an interface** — rejected: the abstraction is unproven until a
  second shape exists, and the first real vendor swap then forces a redesign.
- **Direct vendor calls in the domain code** — rejected: untestable without the vendor,
  and it hard-couples the engine to one provider.
