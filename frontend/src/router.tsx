// Code-based TanStack Router shell: five routes under a shared top-nav layout.
// `createAppRouter` takes an optional history so tests can drive a memory
// history while main.tsx uses the browser default.

import {
  createRootRoute,
  createRoute,
  createRouter,
  redirect,
  type RouterHistory,
} from '@tanstack/react-router'

import { EDITIONS } from './lib/editions'
import { lastRoute } from './lib/lastRoute'
import DashboardPage from './pages/DashboardPage'
import SosPage from './pages/SosPage'
import CoveragePage from './pages/CoveragePage'
import UploadPage from './pages/UploadPage'
import ReportsPage from './pages/ReportsPage'
import QboPage from './pages/QboPage'
import EmployeesPage from './pages/EmployeesPage'
import KioskDevicesPage from './pages/KioskDevicesPage'
import KioskPage from './pages/KioskPage'
import PropertyConfigPage from './pages/PropertyConfigPage'
import PerformancePage from './pages/PerformancePage'
import TimecardsPage from './pages/TimecardsPage'
import PayRunsPage from './pages/PayRunsPage'
import PayrollDashboardPage from './pages/PayrollDashboardPage'
import SchedulePage from './pages/SchedulePage'
import PreviewPage from './pages/PreviewPage'
import { CallbackPage, RootShell } from './RootShell'

const rootRoute = createRootRoute({ component: RootShell })

const callbackRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/callback',
  component: CallbackPage,
})

/**
 * SOS picker state lives in the URL so statement views are linkable:
 * `?property=HISJ&date=2026-07-07` or `?property=HISJ&from=...&to=...`.
 */
export type SosSearch = {
  property?: string
  date?: string
  from?: string
  to?: string
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' && value !== '' ? value : undefined
}

const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/dashboard',
  component: DashboardPage,
})

/**
 * '/' is not a page — it is the entry point. It hands back the page you were
 * last on (a reload of the bare origin, or the post-login bounce, which always
 * returns to '/'), and on a first visit it opens the dashboard. `replace` keeps
 * the entry route out of history so Back never bounces through it again.
 */
const entryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  beforeLoad: () => {
    throw redirect({ href: lastRoute(), replace: true })
  },
})

const sosRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/sos',
  component: SosPage,
  validateSearch: (search: Record<string, unknown>): SosSearch => ({
    property: optionalString(search.property),
    date: optionalString(search.date),
    from: optionalString(search.from),
    to: optionalString(search.to),
  }),
})

/**
 * Coverage edition lives in the URL so worklist views are linkable:
 * `?edition=12`. Optional — the page defaults to edition 12 — so nav links
 * don't have to pass a search object.
 */
export type CoverageSearch = {
  edition?: number
}

const coverageRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/coverage',
  component: CoveragePage,
  validateSearch: (search: Record<string, unknown>): CoverageSearch => {
    // Clamp to known editions: an unknown ?edition= falls back to the page
    // default (12) instead of rendering a blank select.
    const edition = Number(search.edition)
    return { edition: EDITIONS.includes(edition) ? edition : undefined }
  },
})

const uploadRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/upload',
  component: UploadPage,
})

/**
 * Reports and QBO picker state lives in the URL so views are linkable:
 * `?property=HISJ&month=2026-07`. A malformed `month` (not `YYYY-MM`) falls
 * back to "not picked" instead of firing a doomed request.
 */
export type PropertyMonthSearch = {
  property?: string
  month?: string
}

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/

function validatePropertyMonth(search: Record<string, unknown>): PropertyMonthSearch {
  const month = search.month
  return {
    property: optionalString(search.property),
    month: typeof month === 'string' && MONTH_RE.test(month) ? month : undefined,
  }
}

const reportsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/reports',
  component: ReportsPage,
  validateSearch: validatePropertyMonth,
})

const qboRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/qbo',
  component: QboPage,
  validateSearch: validatePropertyMonth,
})

const employeesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/employees',
  component: EmployeesPage,
})

const kioskRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/kiosk',
  component: KioskPage,
})

const timecardsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/timecards',
  component: TimecardsPage,
})

const kioskDevicesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/kiosk-devices',
  component: KioskDevicesPage,
})

const propertyConfigRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/property-config',
  component: PropertyConfigPage,
})

const performanceRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/performance',
  component: PerformancePage,
})

const payrollRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/payroll',
  component: PayRunsPage,
})

const payrollDashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/payroll-dashboard',
  component: PayrollDashboardPage,
})

const scheduleRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/schedule',
  component: SchedulePage,
})

const tryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/try',
  component: PreviewPage,
})

const routeTree = rootRoute.addChildren([
  callbackRoute,
  entryRoute,
  dashboardRoute,
  sosRoute,
  coverageRoute,
  uploadRoute,
  reportsRoute,
  qboRoute,
  employeesRoute,
  kioskRoute,
  timecardsRoute,
  kioskDevicesRoute,
  propertyConfigRoute,
  performanceRoute,
  payrollRoute,
  payrollDashboardRoute,
  scheduleRoute,
  tryRoute,
])

export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history })
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>
  }
}
