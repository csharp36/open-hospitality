// Playwright globalSetup: seed the browser's oidc-client-ts store with tokens
// minted by the e2e backend, so the SPA is authenticated before its first
// paint (RequireAuth passes) and the API bearer reaches the gated backend.
// Runs AFTER the webServers are ready, so scripts/e2e_backend.py has already
// written `.e2e-token` (operator, role=accountant) and `.e2e-admin-token`
// (role=org_admin, for the onboarding e2e in employees.spec.ts) at the repo
// root.
//
// Each token is injected as a serialized oidc-client-ts `User` under the exact
// localStorage key UserManager reads: `oidc.user:${authority}:${client_id}`
// (default authority http://localhost:9080/realms/usali, client operator-portal
// — no VITE overrides in the e2e run). We write a Playwright storageState JSON
// per token; config `use.storageState` loads the operator one into every test
// context by default, and employees.spec.ts overrides it per-file with the
// admin one via `test.use({ storageState })`. Playwright applies the
// localStorage entry when a page navigates to the SPA origin.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const REPO_ROOT = fileURLToPath(new URL('../../', import.meta.url))

// Must match frontend/src/auth/oidc.ts defaults and the SPA dev origin.
const AUTHORITY = 'http://localhost:9080/realms/usali'
const CLIENT_ID = 'operator-portal'
const ORIGIN = 'http://localhost:5173'
const STORAGE_KEY = `oidc.user:${AUTHORITY}:${CLIENT_ID}`

/** Read a minted token file and write a Playwright storageState seeding the
 * oidc-client-ts store with it, at `outputRelativePath` (relative to this
 * file). The roles live server-side on the token itself (/api/me resolves
 * them) — the browser only needs to carry the access_token, so `profile` can
 * stay minimal for every caller. */
function writeStorageStateFromToken(
  tokenFileName: string,
  outputRelativePath: string,
): void {
  const tokenFile = `${REPO_ROOT}${tokenFileName}`
  const accessToken = readFileSync(tokenFile, 'utf8').trim()
  if (!accessToken) throw new Error(`empty token in ${tokenFile}`)

  // Minimal oidc-client-ts User (matches User.toStorageString's shape). A
  // future expires_at makes User.expired === false, so getAccessToken()
  // returns the token and AuthProvider treats the session as valid.
  const user = {
    access_token: accessToken,
    token_type: 'Bearer',
    profile: { sub: 'user-1', preferred_username: 'e2e' },
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    scope: 'openid profile',
  }

  const storageState = {
    cookies: [],
    origins: [
      {
        origin: ORIGIN,
        localStorage: [{ name: STORAGE_KEY, value: JSON.stringify(user) }],
      },
    ],
  }
  const outputPath = fileURLToPath(new URL(outputRelativePath, import.meta.url))
  writeFileSync(outputPath, JSON.stringify(storageState, null, 2))
}

export default function globalSetup(): void {
  mkdirSync(fileURLToPath(new URL('./.auth/', import.meta.url)), { recursive: true })
  writeStorageStateFromToken('.e2e-token', './.auth/storageState.json')
  writeStorageStateFromToken('.e2e-admin-token', './.auth/adminState.json')
  // payroll_admin session for the sealed-PII vault e2e (payroll.spec.ts).
  writeStorageStateFromToken('.e2e-payroll-token', './.auth/payrollState.json')
}
