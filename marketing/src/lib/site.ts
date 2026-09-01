/**
 * The app the site sends visitors to. One constant, so moving the app host or
 * the marketing host is a config change rather than a search-and-replace.
 */
export const APP_ORIGIN = import.meta.env.APP_ORIGIN ?? 'https://demo.mandati.ai'

/** The /try path at APP_ORIGIN. */
export const TRY_URL = `${APP_ORIGIN}/try`

/** APP_ORIGIN with no path — the app root. */
export const LOGIN_URL = APP_ORIGIN
