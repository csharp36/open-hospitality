// USALI editions the API can report against. Shared by the coverage route's
// search-param clamp (router.tsx) and the CoveragePage selector — lives here,
// not in either of them, to avoid a router <-> page value cycle and keep
// component files fast-refresh clean.

export const EDITIONS = [11, 12]
export const DEFAULT_EDITION = 12
