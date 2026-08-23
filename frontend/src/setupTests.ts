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

// Node 22+ ships its own Web Storage globals, and they are present but
// UNDEFINED unless `--localstorage-file` is passed:
//   ExperimentalWarning: localStorage is not available because
//   --localstorage-file was not provided
//
// That built-in accessor WINS over jsdom: vitest installs jsdom's globals onto
// globalThis (so `window === globalThis` here), but it does not replace a
// global that already exists. The net effect under Node >= 24 is that BOTH
// `localStorage` and `window.localStorage` are undefined, and every bare
// `localStorage.getItem(...)` in app code throws "Cannot read properties of
// undefined" — 158 of 300 tests, once the whole component tree fails to mount.
//
// On Node 22 there was no built-in global and the bare reference resolved to
// jsdom's, which is why this only surfaced when CI started tracking the
// Dockerfile's Node major. It is a TEST-environment collision only: in a
// browser the bare global IS Web Storage, and `vite build` never runs this
// code, so the shipped bundle is unaffected.
//
// Forwarding to `window.localStorage` does not work — it is the same undefined
// accessor. Supply a real one. A fresh instance per test FILE matches jsdom's
// own semantics (one window per file), so isolation is unchanged.
class MemoryStorage {
  private map = new Map<string, string>()
  get length(): number {
    return this.map.size
  }
  key(i: number): string | null {
    return [...this.map.keys()][i] ?? null
  }
  getItem(k: string): string | null {
    return this.map.get(k) ?? null
  }
  setItem(k: string, v: string): void {
    this.map.set(k, String(v))
  }
  removeItem(k: string): void {
    this.map.delete(k)
  }
  clear(): void {
    this.map.clear()
  }
}

if (typeof globalThis.localStorage === 'undefined') {
  globalThis.localStorage = new MemoryStorage() as unknown as Storage
}
if (typeof globalThis.sessionStorage === 'undefined') {
  globalThis.sessionStorage = new MemoryStorage() as unknown as Storage
}
