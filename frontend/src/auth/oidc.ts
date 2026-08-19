// OIDC (Authorization Code + PKCE) against the Keycloak `usali` realm.
// Config comes from Vite env so the deployed issuer is a build-time swap:
//   VITE_OIDC_AUTHORITY, VITE_OIDC_CLIENT_ID
import { UserManager, WebStorageStateStore, type User } from 'oidc-client-ts'

const authority =
  import.meta.env.VITE_OIDC_AUTHORITY ?? 'http://localhost:9080/realms/usali'
const client_id = import.meta.env.VITE_OIDC_CLIENT_ID ?? 'operator-portal'

// KNOWN LIMITATION (A1): no automatic token refresh. automaticSilentRenew and
// monitorSession default to false, and scope 'openid profile' omits
// offline_access — so Keycloak issues no refresh token and sessions simply
// expire. When silent renew is enabled later, add offline_access (or use
// prompt=none via the session iframe) and turn on automaticSilentRenew.
export const userManager = new UserManager({
  authority,
  client_id,
  redirect_uri: `${window.location.origin}/callback`,
  post_logout_redirect_uri: window.location.origin,
  response_type: 'code',
  scope: 'openid profile',
  userStore: new WebStorageStateStore({ store: window.localStorage }),
})

export function login(loginHint?: string): Promise<void> {
  return loginHint
    ? userManager.signinRedirect({ login_hint: loginHint })
    : userManager.signinRedirect()
}
export function logout(): Promise<void> {
  return userManager.signoutRedirect()
}
export function getUser(): Promise<User | null> {
  return userManager.getUser()
}
export async function getAccessToken(): Promise<string | null> {
  const u = await userManager.getUser()
  return u && !u.expired ? u.access_token : null
}
