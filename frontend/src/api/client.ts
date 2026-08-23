// Typed fetch wrappers over the portal API (src/usali/portal_api.py) and the
// pre-existing POST /ingest. Non-2xx responses throw `ApiError` carrying the
// HTTP status and the FastAPI `detail` message.

import type {
  AdrRoomBasis,
  Department,
  AvailabilityNote,
  CoverageReport,
  CpaPack,
  DemandSurface,
  Employee,
  EmployeeUpdate,
  EmployeeWork,
  FaceTemplateEnrolled,
  FaceTemplateStatus,
  FetchResultsResponse,
  FiscalConfig,
  FiscalPeriod,
  ForecastDay,
  ForecastSaved,
  ForecastUpsertBody,
  IngestResult,
  InventoryRow,
  JePlan,
  EnrolledKiosk,
  KioskConfig,
  KioskDeviceInfo,
  KioskEmployee,
  KioskIdentifyResult,
  LaborAnalytics,
  KioskMyWeek,
  KioskPunchState,
  KioskRosterEmployee,
  DepositAccountsBody,
  DepositAccountsStatus,
  LaborStandard,
  Me,
  Onboarded,
  OnboardEmployeeRequest,
  OooRow,
  PayrollProfileStatus,
  PayrollPublicKey,
  PayRunCreateBody,
  PayRunDetail,
  PayRunLines,
  PayRunSummary,
  PayRate,
  PerformanceResponse,
  PropertyConfig,
  PropertyInfo,
  Punched,
  PunchType,
  PushLedgerRow,
  PushResult,
  ScheduleAdherence,
  ScheduleProjection,
  ScheduleShift,
  ScheduleTargets,
  ScheduleWeek,
  ScheduleWeekCreateBody,
  SealedProfileBody,
  SetPayRateBody,
  ShiftCreateBody,
  ShiftTemplate,
  SosReport,
  StagedTxn,
  StandardUpsertBody,
  TemplateCreateBody,
  Timecard,
  TimecardSummary,
  PreviewResponse,
} from './types'
import { getAccessToken, login } from '../auth/oidc'
import { orgAliasesFromToken, resolveActiveOrg } from '../auth/activeOrg'

// THE single header seam (Pillar L decision 6): every operator request goes
// through here, so both the bearer token AND the active-org header are set in
// exactly one place. X-Active-Org names the alias the server validates against
// the token's memberships (L3). A single-org user's one org resolves
// automatically; a multi-org user carries their picked alias; an unresolved
// multi-org request carries no header (the server 400s ambiguity, never
// guesses). The kiosk device calls deliberately bypass authHeaders() and so
// carry neither header — kiosk multi-org is deferred.
async function authHeaders(base?: HeadersInit): Promise<Headers> {
  const headers = new Headers(base)
  const token = await getAccessToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const active = resolveActiveOrg(orgAliasesFromToken(token))
  if (active) headers.set('X-Active-Org', active)
  return headers
}

// A1 has no token refresh, so an expired session surfaces as 401s across every
// in-flight query. Guard against hammering signinRedirect() (redundant PKCE/
// state writes): trigger login at most once — the redirect reloads the page,
// so no reset is needed.
let redirecting = false

function redirectToLogin(): void {
  if (!redirecting) {
    redirecting = true
    void login()
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  /**
   * The raw FastAPI `detail` before stringification. Usually the same string
   * as `detail`, but structured 422 bodies — the QBO unmapped-GL worklist
   * `{"unmapped": [...]}` — arrive here as the parsed object.
   */
  readonly detailBody: unknown

  constructor(status: number, detail: string, detailBody?: unknown) {
    super(`HTTP ${status}: ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.detailBody = detailBody ?? detail
  }
}

async function raiseApiError(res: Response): Promise<never> {
  let detail = res.statusText
  let detailBody: unknown = res.statusText
  try {
    const body: unknown = await res.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail
      detail = typeof d === 'string' ? d : JSON.stringify(d)
      detailBody = d
    }
  } catch {
    // Non-JSON error body: keep the status text.
  }
  throw new ApiError(res.status, detail, detailBody)
}

async function getJson<T>(
  path: string,
  params?: Record<string, string | undefined>,
): Promise<T> {
  const entries = params
    ? Object.entries(params).filter((e): e is [string, string] => e[1] !== undefined)
    : []
  const qs = entries.length > 0 ? `?${new URLSearchParams(entries)}` : ''
  const res = await fetch(`${path}${qs}`, { headers: await authHeaders() })
  if (res.status === 401) {
    redirectToLogin()
    await raiseApiError(res)
  }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<T>
}

/**
 * SOS date modes mirror the API contract: single `date` XOR `from`+`to`.
 * The `never` guards make passing both modes (or a lone `from`/`to`) a
 * compile-time error, matching the endpoint's 422 validation.
 */
export type SosParams = { property: string } & (
  | { date: string; from?: never; to?: never }
  | { date?: never; from: string; to: string }
)

// Type alias (not interface) so it satisfies the Record-typed getJson params —
// interfaces lack the implicit index signature that object literal types get.
export type LineTransactionsParams = {
  property: string
  major: string
  sub: string
  line_item: string
  from: string
  to: string
}

export function getProperties(): Promise<PropertyInfo[]> {
  return getJson('/api/properties')
}

export function getSos(params: SosParams): Promise<SosReport> {
  return getJson('/api/sos', params)
}

export function getLineTransactions(params: LineTransactionsParams): Promise<StagedTxn[]> {
  return getJson('/api/sos/line/transactions', params)
}

export function getCoverage(edition: number = 12): Promise<CoverageReport> {
  return getJson('/api/coverage', { edition: String(edition) })
}

// --- P8: CPA pack + QBO push ------------------------------------------------
// Request params/bodies use the API's `property`/`date`/`month` aliases;
// responses come back with `property_id`/`business_date`. That asymmetry is
// the backend contract (Field aliases in portal_api.py), mirrored on purpose.

export function getCpaPack(property: string, month: string): Promise<CpaPack> {
  return getJson('/api/cpa-pack', { property, month })
}

// Type alias (not interface) for the same getJson-params reason as above.
export type QboStatusParams = {
  property?: string
  month?: string
}

export function getQboStatus(params: QboStatusParams = {}): Promise<PushLedgerRow[]> {
  return getJson('/api/qbo/status', params)
}

/**
 * The balanced JE plan for one property + business date; posts nothing.
 * Incomplete GL curation rejects with a 422 ApiError whose `detailBody` is
 * the structured `{"unmapped": [...]}` worklist.
 */
export function getQboPreview(property: string, date: string): Promise<JePlan> {
  return getJson('/api/qbo/preview', { property, date })
}

/**
 * Push one property+date JE to QBO — the portal's only write besides /ingest.
 * NOTE: `stale` and `failed` outcomes resolve (HTTP 200) — callers must
 * branch on `PushResult.status`; only transport/validation failures reject
 * (422 unmapped worklist, 502 QBO unreachable).
 */
export async function postQboPush(property: string, date: string): Promise<PushResult> {
  const res = await fetch('/api/qbo/push', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ property, date }),
  })
  if (res.status === 401) {
    redirectToLogin()
    await raiseApiError(res)
  }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<PushResult>
}

// --- Employees / onboarding (A2.3) ------------------------------------------

export function getEmployees(includeInactive = false): Promise<Employee[]> {
  return getJson(`/api/employees${includeInactive ? '?include_inactive=true' : ''}`)
}

/** Labor cost and hours over a window, per day and per department. One call
 *  powers the whole payroll dashboard — the day series is computed server-side
 *  so the suppression rule stays in one place. */
export function getLaborAnalytics(params: {
  property: string
  from: string
  to: string
}): Promise<LaborAnalytics> {
  return getJson('/api/labor/analytics', params)
}

/** Hours + estimated cost per employee over a window. Scope-filtered server
 *  side: an employee outside the caller's scope is absent, not zeroed. */
export function getEmployeeWork(params: {
  from: string
  to: string
  property?: string
}): Promise<EmployeeWork[]> {
  const q = new URLSearchParams({ from: params.from, to: params.to })
  if (params.property !== undefined) q.set('property', params.property)
  return getJson(`/api/employees/work?${q.toString()}`)
}

export async function updateEmployee(
  employeeId: number,
  body: EmployeeUpdate,
): Promise<Employee> {
  const res = await fetch(`/api/employees/${employeeId}`, {
    method: 'PATCH',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<Employee>
}

/** The hourly rate on the employee's PRIMARY placement. payroll_admin only,
 *  and the server writes a read_pay_rate audit row on every call — invoke only
 *  from an explicit user action (opening the edit modal). */
export function getPayRate(employeeId: number): Promise<PayRate> {
  return getJson(`/api/employees/${employeeId}/pay-rate`)
}

export async function setPayRate(employeeId: number, body: SetPayRateBody): Promise<PayRate> {
  const res = await fetch(`/api/employees/${employeeId}/pay-rate`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<PayRate>
}

/** Suspend / reinstate. Suspension keeps the placement but puts it in force on
 *  no date, so scheduling, timecards and pay runs all drop the employee. */
export async function setEmployeeSuspended(
  employeeId: number,
  suspended: boolean,
): Promise<{ employee_id: number; status: string }> {
  const res = await fetch(
    `/api/employees/${employeeId}/${suspended ? 'suspend' : 'reinstate'}`,
    { method: 'POST', headers: await authHeaders() },
  )
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<{ employee_id: number; status: string }>
}

export function getMe(): Promise<Me> {
  return getJson('/api/me')
}

export async function onboardEmployee(body: OnboardEmployeeRequest): Promise<Onboarded> {
  const res = await fetch('/api/employees', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<Onboarded>
}

export async function terminateEmployee(employeeId: number): Promise<{ employee_id: number }> {
  const res = await fetch(`/api/employees/${employeeId}/terminate`, {
    method: 'POST',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<{ employee_id: number }>
}

// --- Payroll PII vault (Pillar C1) ------------------------------------------
// Client-side-sealed PII: the browser fetches the pinned recipient public key,
// seals each field with @hpke/core (lib/piiSeal.ts), and PUTs opaque envelopes.
// The vault has no read path — getPayrollProfile returns on-file flags only.

export function getPayrollPublicKey(): Promise<PayrollPublicKey> {
  return getJson('/api/payroll/pii-public-key')
}

export function getPayrollProfile(employeeId: number): Promise<PayrollProfileStatus> {
  return getJson(`/api/payroll/employees/${employeeId}/profile`)
}

export async function putPayrollProfile(
  employeeId: number,
  body: SealedProfileBody,
): Promise<PayrollProfileStatus> {
  const res = await fetch(`/api/payroll/employees/${employeeId}/profile`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<PayrollProfileStatus>
}

// E5: the deposit destination is a CHAIN. PUT replaces the whole chain — the
// client seals every account fresh (full re-entry + re-seal, the C1 contract).

export function getDepositAccounts(employeeId: number): Promise<DepositAccountsStatus> {
  return getJson(`/api/payroll/employees/${employeeId}/deposit-accounts`)
}

export async function putDepositAccounts(
  employeeId: number,
  body: DepositAccountsBody,
): Promise<DepositAccountsStatus> {
  const res = await fetch(`/api/payroll/employees/${employeeId}/deposit-accounts`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<DepositAccountsStatus>
}

// --- Pay runs (Pillar C2) ------------------------------------------------------
// Payroll-Admin-gated (require_payroll_admin composes on the vault's pattern).
// A 422 from createPayRun carries the preflight blocker list as its `detail`
// string — PayRunsPage renders it verbatim (named blockers are the feature).

export function getPayRuns(property: string): Promise<PayRunSummary[]> {
  return getJson('/api/payroll/runs', { property })
}

export function getPayRun(id: number): Promise<PayRunDetail> {
  return getJson(`/api/payroll/runs/${id}`)
}

export function getPayRunLines(id: number): Promise<PayRunLines> {
  // The server writes a read_pay_run_lines audit row on EVERY call (C3's
  // compensation gate) — invoke only from an explicit user action.
  return getJson(`/api/payroll/runs/${id}/lines`)
}

export async function createPayRun(body: PayRunCreateBody): Promise<PayRunSummary> {
  const res = await fetch('/api/payroll/runs', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<PayRunSummary>
}

export async function fetchPayRunResults(id: number): Promise<FetchResultsResponse> {
  const res = await fetch(`/api/payroll/runs/${id}/fetch-results`, {
    method: 'POST',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<FetchResultsResponse>
}

// --- Timecards (B2) ----------------------------------------------------------
// Operator calls (approver-only on the server), so unlike the kiosk they carry
// the session token via authHeaders().

export function getTimecards(): Promise<TimecardSummary[]> {
  return getJson('/api/timecards')
}

export function getTimecard(id: number): Promise<Timecard> {
  return getJson(`/api/timecards/${id}`)
}

/**
 * The punch's evidence photo as a Blob. Every successful call is AUDITED
 * server-side as "this manager saw this face" — so callers must fetch on an
 * explicit user action (a Photo button), never eagerly for a whole list.
 */
export async function getPunchPhoto(punchId: number): Promise<Blob> {
  const res = await fetch(`/api/punches/${punchId}/photo`, {
    headers: await authHeaders(),
  })
  if (res.status === 401) {
    redirectToLogin()
    await raiseApiError(res)
  }
  if (!res.ok) await raiseApiError(res)
  return res.blob()
}

export async function approveTimecard(
  id: number,
  // F6: the unverified punch ids the approver is explicitly vouching for —
  // the server refuses the approval (409) if any red punch is missing here,
  // and audits each acknowledgment by punch.
  acknowledgedPunchIds: number[] = [],
): Promise<Timecard> {
  const res = await fetch(`/api/timecards/${id}/approve`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ acknowledged_punch_ids: acknowledgedPunchIds }),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<Timecard>
}

/**
 * H3: reopen an approved card for a fresh review. Approver-only and audited
 * server-side; relinks any punches that landed after approval, so re-approval
 * reviews them (red badges must be re-acknowledged — nothing carries over).
 */
export async function reopenTimecard(id: number): Promise<Timecard> {
  const res = await fetch(`/api/timecards/${id}/reopen`, {
    method: 'POST',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<Timecard>
}

// --- Scheduling (Pillar D1) --------------------------------------------------
// GM/org-admin only on the server (require_scheduler + assignment_scope
// confinement). Request bodies use the `property` alias; responses come back
// with `property_id` — the established backend asymmetry, mirrored on purpose.
// Times travel as "HH:MM" strings both ways. The server is authoritative on
// the payroll Monday grid: createScheduleWeek 422s an off-grid week_start and
// the page renders that detail verbatim rather than duplicating the anchor.

export function getDepartments(property: string): Promise<Department[]> {
  return getJson('/api/departments', { property })
}

export async function createDepartment(body: {
  property_id: string
  name: string
}): Promise<Department> {
  const res = await fetch('/api/departments', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res) // 409 on duplicate name
  return res.json() as Promise<Department>
}

export function getTemplates(property: string): Promise<ShiftTemplate[]> {
  return getJson('/api/schedule/templates', { property })
}

export async function createTemplate(body: TemplateCreateBody): Promise<ShiftTemplate> {
  const res = await fetch('/api/schedule/templates', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<ShiftTemplate>
}

export async function deleteTemplate(templateId: number): Promise<void> {
  const res = await fetch(`/api/schedule/templates/${templateId}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res) // 409 when shifts reference it
}

/** 404s (ApiError.status === 404) when the week has not been created yet —
 * the page turns that into the "Create week" button, not an error banner. */
export function getScheduleWeek(property: string, weekStart: string): Promise<ScheduleWeek> {
  return getJson('/api/schedule/weeks', { property, week_start: weekStart })
}

export async function createScheduleWeek(body: ScheduleWeekCreateBody): Promise<ScheduleWeek> {
  const res = await fetch('/api/schedule/weeks', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<ScheduleWeek>
}

export async function createShift(
  scheduleId: number,
  body: ShiftCreateBody,
): Promise<ScheduleShift> {
  const res = await fetch(`/api/schedule/weeks/${scheduleId}/shifts`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<ScheduleShift>
}

export async function updateShift(
  shiftId: number,
  body: ShiftCreateBody,
): Promise<ScheduleShift> {
  const res = await fetch(`/api/schedule/shifts/${shiftId}`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<ScheduleShift>
}

export async function deleteShift(shiftId: number): Promise<void> {
  const res = await fetch(`/api/schedule/shifts/${shiftId}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}

/** Live projection for the week — per-employee HOURS only, department-
 * aggregate cost with suppression (see types.ts). Consumed by Task 6's panel. */
export function getProjection(scheduleId: number): Promise<ScheduleProjection> {
  return getJson(`/api/schedule/weeks/${scheduleId}/projection`)
}

/** Flip draft → published (republish bumps `version`); audited server-side. */
export async function publishSchedule(scheduleId: number): Promise<ScheduleWeek> {
  const res = await fetch(`/api/schedule/weeks/${scheduleId}/publish`, {
    method: 'POST',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<ScheduleWeek>
}

// --- Scheduling targets / forecast / standards (Pillar D2) -------------------
// Same require_scheduler + property-confinement gates as the D1 routes above.
// THE MONEY RULE carried to the client: targets and standards are HOURS-only —
// none of these calls can return or send a money value.

export function getStandards(property: string): Promise<LaborStandard[]> {
  return getJson('/api/schedule/standards', { property })
}

/** Upsert BY DEPARTMENT (one standard per department); re-PUT replaces basis
 * and value. An unknown basis or value <= 0 is a 422 straight from the schema. */
export async function upsertStandard(body: StandardUpsertBody): Promise<LaborStandard> {
  const res = await fetch('/api/schedule/standards', {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<LaborStandard>
}

export async function deleteStandard(standardId: number): Promise<void> {
  const res = await fetch(`/api/schedule/standards/${standardId}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}

/** 7 rows for the week — the GM's saved numbers plus history HINTS (same-day-
 * last-week, trailing average) from our own promoted ROOMS_OCCUPIED facts.
 * Hints are null (never 0) when the facts are absent, and inform, never
 * dictate: the UI must never auto-fill an input from a hint. */
export function getForecast(property: string, weekStart: string): Promise<ForecastDay[]> {
  return getJson('/api/schedule/forecast', { property, week_start: weekStart })
}

export async function saveForecast(body: ForecastUpsertBody): Promise<ForecastSaved> {
  const res = await fetch('/api/schedule/forecast', {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<ForecastSaved>
}

/** Target vs scheduled HOURS per department per day. A per-room day without a
 * forecast targets null, NOT zero (absence is not zero demand) — the
 * days_without_forecast counter tells the truth about the gap. */
export function getTargets(scheduleId: number): Promise<ScheduleTargets> {
  return getJson(`/api/schedule/weeks/${scheduleId}/targets`)
}

/** CRM demand for the window (Pillar J): the LATEST snapshot per stay date,
 * capability-labeled. Hints inform, never dictate — nothing here may move a
 * target or fill an input. configured:false = the feature is off; render no
 * demand UI at all (an honest gap, not an error). */
export function getDemand(
  property: string,
  start: string,
  end: string,
): Promise<DemandSurface> {
  return getJson('/api/crm/demand', { property, start, end })
}

/** Scheduled-vs-punched per department/day for the week's ELAPSED days
 * (strictly before `asOf`; omitted = server today) plus the exceptions list
 * (no_show / unscheduled_punch / deviation). HOURS ONLY — THE MONEY RULE.
 * Punches change server-side independently of anything this page mutates,
 * so the panel refreshes on demand rather than relying on invalidation. */
export function getAdherence(
  scheduleId: number,
  asOf?: string,
): Promise<ScheduleAdherence> {
  return getJson(`/api/schedule/weeks/${scheduleId}/adherence`, { as_of: asOf })
}

/** GM-maintained scheduling aid ("can't work Tuesdays") — operational, never
 * money/medical. null clears the note. The updated note travels back on the
 * roster (getEmployees), so callers invalidate ['employees']. */
export async function putAvailabilityNote(
  employeeId: number,
  note: string | null,
): Promise<AvailabilityNote> {
  const res = await fetch(`/api/schedule/employees/${employeeId}/availability-note`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ note }),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<AvailabilityNote>
}

// --- Property configuration: room inventory & fiscal calendar (issue #8) ----
// org_admin | property_gm (confined to assigned properties) on the server via
// require_grants(ORG_ADMIN, PROPERTY_GM) + _require_onboardable_property; reads
// gate on _require_readable_property. Every write is audited server-side.

export function getPropertyConfig(propertyId: string): Promise<PropertyConfig> {
  return getJson(`/api/properties/${propertyId}/config`)
}

export async function setInventory(
  propertyId: string,
  body: { effective_date: string; total_rooms: number },
): Promise<InventoryRow> {
  const res = await fetch(`/api/properties/${propertyId}/inventory`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<InventoryRow>
}

export async function addOoo(
  propertyId: string,
  body: Omit<OooRow, 'ooo_id'>,
): Promise<OooRow> {
  const res = await fetch(`/api/properties/${propertyId}/out-of-order`, {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<OooRow>
}

export async function removeOoo(propertyId: string, oooId: number): Promise<void> {
  const res = await fetch(`/api/properties/${propertyId}/out-of-order/${oooId}`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}

export async function setFiscalCalendar(
  propertyId: string,
  body: FiscalConfig,
): Promise<FiscalConfig> {
  const res = await fetch(`/api/properties/${propertyId}/fiscal-calendar`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<FiscalConfig>
}

export function getFiscalPeriods(
  propertyId: string,
  fiscalYear: number,
): Promise<{ periods: FiscalPeriod[] }> {
  return getJson(`/api/properties/${propertyId}/fiscal-periods`, {
    fiscal_year: String(fiscalYear),
  })
}

// --- Core performance statistics (issue #9) ---------------------------------
// GET /api/performance is property-access gated server-side; the window is
// given EITHER by `from`+`to` OR by `period=YYYY-Pnn` — the union type makes
// mixing the two a compile-time error, mirroring the endpoint's exactly-one-form
// 422. setStatConfig is the config-writer-gated ADR-basis setter from Task A2
// (the same /api/properties router as the issue #8 config writes above).

export function getPerformance(
  property: string,
  opts: { from: string; to: string } | { period: string },
): Promise<PerformanceResponse> {
  return getJson('/api/performance', { property, ...opts })
}

export async function setStatConfig(
  property: string,
  adr_room_basis: AdrRoomBasis,
): Promise<{ adr_room_basis: AdrRoomBasis }> {
  const res = await fetch(`/api/properties/${property}/stat-config`, {
    method: 'PUT',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ adr_room_basis }),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<{ adr_room_basis: AdrRoomBasis }>
}

export async function postIngest(file: File): Promise<IngestResult> {
  const form = new FormData()
  form.append('file', file, file.name)
  const res = await fetch('/ingest', {
    method: 'POST',
    headers: await authHeaders(),
    body: form,
  })
  if (res.status === 401) {
    redirectToLogin()
    await raiseApiError(res)
  }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<IngestResult>
}

// --- Public preview (anonymous front door, Part 1 backend) ------------------
// POST /api/preview is the unauthenticated on-ramp: a visitor drops a PDF
// before ever creating an account.

/**
 * PUBLIC preview — the anonymous front door. Posts the raw PDF body to the
 * Part 1 endpoint. Deliberately does NOT attach the OIDC token (authHeaders)
 * and does NOT redirect to login on a 4xx: an anonymous visitor must never be
 * bounced to Keycloak. 413/415/429 surface as ApiError for a friendly message.
 */
export async function postPreview(file: File): Promise<PreviewResponse> {
  const res = await fetch('/api/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/pdf' },
    body: file, // a File is a Blob -> sent as the raw request body
  })
  if (!res.ok) await raiseApiError(res)
  return (await res.json()) as PreviewResponse
}

// --- Face enrollment (Pillar F, operator session) -----------------------------
// Onboarder-gated server-side (org_admin | property_gm with whole-person
// scope). The client sends a JPEG; the server embeds and keeps the template —
// the vector never crosses the wire in either direction.

export function getFaceTemplate(employeeId: number): Promise<FaceTemplateStatus> {
  return getJson(`/api/employees/${employeeId}/face-template`)
}

export async function enrollFaceTemplate(
  employeeId: number,
  photo: Blob,
): Promise<FaceTemplateEnrolled> {
  const form = new FormData()
  form.set('photo', photo, 'face.jpg')
  const res = await fetch(`/api/employees/${employeeId}/face-template`, {
    method: 'POST',
    headers: await authHeaders(), // no Content-Type: browser sets the multipart boundary
    body: form,
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<FaceTemplateEnrolled>
}

export async function deleteFaceTemplate(employeeId: number): Promise<void> {
  const res = await fetch(`/api/employees/${employeeId}/face-template`, {
    method: 'DELETE',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
}

// --- Kiosk device admin (operator session, org_admin | property_gm) ----------

export function getKioskDevices(): Promise<KioskDeviceInfo[]> {
  return getJson('/api/kiosk-devices')
}

export async function enrollKioskDevice(
  property: string,
  name: string,
): Promise<EnrolledKiosk> {
  const res = await fetch('/api/kiosk-devices', {
    method: 'POST',
    headers: await authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ property, name }),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<EnrolledKiosk>
}

export async function revokeKioskDevice(deviceId: number): Promise<KioskDeviceInfo> {
  const res = await fetch(`/api/kiosk-devices/${deviceId}/revoke`, {
    method: 'POST',
    headers: await authHeaders(),
  })
  if (res.status === 401) { redirectToLogin(); await raiseApiError(res) }
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<KioskDeviceInfo>
}

// --- Kiosk time capture (B1) -------------------------------------------------
// The kiosk is authenticated by its DEVICE token, never by an operator session —
// so these calls deliberately bypass authHeaders()/redirectToLogin().

export async function getKioskRoster(
  deviceToken: string,
): Promise<KioskRosterEmployee[]> {
  const res = await fetch('/api/kiosk/employees', {
    headers: { 'X-Kiosk-Token': deviceToken },
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<KioskRosterEmployee[]>
}

export async function getKioskPunchState(
  deviceToken: string,
  employeeId: number,
): Promise<KioskPunchState> {
  const res = await fetch(`/api/kiosk/punch-state?employee_id=${employeeId}`, {
    headers: { 'X-Kiosk-Token': deviceToken },
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<KioskPunchState>
}

export async function getKioskMyWeek(
  deviceToken: string,
  employeeId: number,
  weekStart: string, // ISO Monday — the kiosk computes the upcoming week's
  // Monday client-side; the server validates and stays dumb (both params
  // REQUIRED there).
): Promise<KioskMyWeek> {
  const params = new URLSearchParams({
    employee_id: String(employeeId),
    week_start: weekStart,
  })
  const res = await fetch(`/api/kiosk/my-week?${params.toString()}`, {
    headers: { 'X-Kiosk-Token': deviceToken },
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<KioskMyWeek>
}

export async function getKioskConfig(deviceToken: string): Promise<KioskConfig> {
  const res = await fetch('/api/kiosk/config', {
    headers: { 'X-Kiosk-Token': deviceToken },
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<KioskConfig>
}

// Photo -> the one candidate for the confirm tap (Pillar F). Stateless on the
// server: the probe is discarded, nothing stored, nothing audited — the punch
// that follows is re-verified server-side from its own photo.
export async function postKioskIdentify(
  deviceToken: string,
  photo: Blob,
): Promise<KioskIdentifyResult> {
  const form = new FormData()
  form.set('photo', photo, 'frame.jpg')
  const res = await fetch('/api/kiosk/identify', {
    method: 'POST',
    headers: { 'X-Kiosk-Token': deviceToken },
    body: form,
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<KioskIdentifyResult>
}

// The no-roster fallback: 3+ characters, this property's active staff only,
// capped server-side — self-identification, never roster browsing.
export async function getKioskSearch(
  deviceToken: string,
  q: string,
): Promise<KioskEmployee[]> {
  const params = new URLSearchParams({ q })
  const res = await fetch(`/api/kiosk/search?${params.toString()}`, {
    headers: { 'X-Kiosk-Token': deviceToken },
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<KioskEmployee[]>
}

export async function postPunch(
  deviceToken: string,
  employeeId: number,
  punchType: PunchType,
  photo: Blob,
): Promise<Punched> {
  const form = new FormData()
  form.set('employee_id', String(employeeId))
  form.set('punch_type', punchType)
  form.set('photo', photo, 'punch.jpg')
  const res = await fetch('/api/kiosk/punch', {
    method: 'POST',
    headers: { 'X-Kiosk-Token': deviceToken }, // no Content-Type: browser sets the multipart boundary
    body: form,
  })
  if (!res.ok) await raiseApiError(res)
  return res.json() as Promise<Punched>
}
