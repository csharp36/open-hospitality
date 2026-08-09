/// <reference types="node" />
import { webcrypto } from 'node:crypto'
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// WebCrypto insurance for the client-side HPKE sealing tests (piiSeal.ts): on any
// CI Node that predates a global `crypto`, expose node's WebCrypto so P-256
// subtle is available. Harmless on Node >= 20 where globalThis.crypto is already
// present (the `??=` no-ops).
globalThis.crypto ??= webcrypto as unknown as Crypto

// Vitest runs without `globals: true`, so Testing Library's automatic
// afterEach cleanup never registers — do it explicitly or renders accumulate
// across tests within a file.
afterEach(cleanup)

// jsdom does not implement window.scrollTo; TanStack Router calls it on
// navigation. Stub it to silence the "Not implemented" noise in test output.
window.scrollTo = () => {}
