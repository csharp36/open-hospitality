// TypeScript mirrors of the Pydantic response models in src/usali/portal_api.py
// (field-for-field). Conventions shared with the backend:
//   - Decimal amounts serialize as exact strings, never floats -> typed `string`.
//   - Dates serialize as ISO `YYYY-MM-DD` strings -> typed `string`.

// --- SOS report -------------------------------------------------------------

export interface SosLine {
  major: string
  sub_category: string
  line_item: string
  total: string
}

export interface DeptSection {
  sub_category: string
  lines: SosLine[]
  total: string
}

export interface SegmentLine {
  segment: string
  rooms: string
  room_revenue: string
}

export interface MetricRow {
  metric_code: string
  day: string | null
  mtd: string | null
  ytd: string | null
  day_prior: string | null
  mtd_prior: string | null
  ytd_prior: string | null
}

// Pillar B3: labor promoted from approved timecards, unioned into the SOS.
// Schedule 14 = estimated payroll expense; Schedule 15 = hours/OT/FTE. Amounts
// and hours are exact decimal strings; est_cost is an ESTIMATE.
export interface LaborLine {
  department: string
  hours: string
  ot_hours: string
  // null when the department's cost is suppressed (single-employee rate-
  // derivation guard) — hours still carry.
  est_cost: string | null
}

// Pillar C3: estimate vs provider-actual labor for the processed pay periods
// intersecting the SOS window. Mirrors LaborVarianceLineModel in portal_api.py
// field-for-field. Null money = suppressed (single-employee department, on
// EITHER side) — the burden too, since burden ~ a fixed % of gross would
// re-derive the rate. Hours still carry.
export interface LaborVarianceLine {
  department: string
  est_cost: string | null
  actual_gross: string | null
  employer_burden: string | null
  variance: string | null
  hours_actual: string
  alert: boolean
}

// Mirrors LaborVarianceModel. Estimate/actual cover the FULL pay periods
// listed in `periods` (a period can extend past the SOS window — the labels
// are the honest explanation). Totals EXCLUDE suppressed departments on both
// sides (complementary suppression).
export interface LaborVariance {
  lines: LaborVarianceLine[]
  periods: string[]
  est_total: string
  actual_total: string
  variance_total: string
  burden_total: string
  alert: boolean
  suppressed_departments: number
  unpriced_hours: string
}

export interface SosReport {
  property_id: string
  pms_source: string
  business_date: string | null
  date_from: string | null
  date_to: string | null
  operated_departments: DeptSection[]
  misc_income: SosLine[]
  misc_income_total: string
  total_operating_revenue: string
  taxes: SosLine[]
  taxes_total: string
  settlements: SosLine[]
  settlements_total: string
  other: SosLine[]
  other_total: string
  rooms_segments: SegmentLine[]
  statistics: MetricRow[]
  // Pillar B3: labor sections, ESTIMATES kept outside the operating-revenue
  // reconciliation (labor is expense, not revenue). labor_fte is null when
  // there are no promoted hours.
  payroll_expense: LaborLine[]
  payroll_expense_total: string
  labor_hours_total: string
  labor_ot_hours_total: string
  labor_fte: string | null
  // Count of departments whose cost is hidden (single-employee guard);
  // payroll_expense_total EXCLUDES them. labor_unpriced_hours = hours worked at
  // est_cost 0 (no rate on file) — Schedule 14 flags both honestly.
  labor_suppressed_departments: number
  labor_unpriced_hours: string
  // Pillar C3: estimate vs provider-actual for the pay periods intersecting
  // the window. Null when no processed pay run touches it.
  labor_variance: LaborVariance | null
}

// --- Coverage report --------------------------------------------------------

export interface NeedsReviewEntry {
  code: string
  line_item: string
  notes: string | null
}

export interface FinancialCoverage {
  dictionary_entries: number
  by_confidence: Record<string, number>
  by_review_status: Record<string, number>
  needs_review: NeedsReviewEntry[]
  staged_codes: number
  mapped_codes: number
  missing_codes: string[]
  exception_count: number
  gl_mapped: number
  gl_unmapped_codes: string[]
}

export interface SegmentCoverage {
  dictionary_entries: number
  needs_review: NeedsReviewEntry[]
  staged_codes: number
  mapped_codes: number
  unmapped_codes: string[]
}

export interface StatisticsCoverage {
  dictionary_entries: number
  staged_labels: number
  mapped_labels: number
  unmapped_labels: string[]
}

export interface SourceCoverage {
  pms_source: string
  financial: FinancialCoverage
  segments: SegmentCoverage
  statistics: StatisticsCoverage
}

export interface CoverageReport {
  sources: SourceCoverage[]
}

// --- Drill-through + property picker ---------------------------------------

export interface StagedTxn {
  stage_id: number
  pms_source: string
  business_date: string
  pms_trx_code: string
  pms_trx_desc: string | null
  amount: string
  source_file: string
}

export interface PropertyInfo {
  property_id: string
  pms_source: string
  first_date: string
  last_date: string
  // Registry display name (e.g. "HOLIDAY INN & SUITES SAN JOSE"); null when
  // the id has facts but no registry row.
  name: string | null
}

export interface Department {
  department_id: number
  property_id: string
  name: string
}

// --- CPA monthly pack (P8) ----------------------------------------------------

export interface SalesLine {
  major: string
  sub_category: string
  line_item: string
  mtd_amount: string
  day_count: number
}

export interface SalesReport {
  lines: SalesLine[]
  total_operating_revenue: string
}

export interface TaxLine {
  line_item: string
  gl_account_code: string | null
  mtd_amount: string
}

export interface TaxReport {
  lines: TaxLine[]
  taxes_total: string
  room_revenue_base: string
}

/**
 * `opening_balance` is the FIRST reported balance IN the month, not the prior
 * month's close — with daily trial-balance ingestion the two coincide, but
 * after an ingestion gap the opening is simply the earliest date that
 * reported. (Carried from the backend ArLine docstring; the UI must label
 * this honestly — "First reported", not a bare "Opening".)
 */
export interface ArLine {
  ledger_code: string
  ledger_name: string
  opening_balance: string
  closing_balance: string
  movement: string
}

export interface ArReport {
  balances: ArLine[]
}

export interface CpaPack {
  property_id: string
  pms_source: string
  month: string
  sales: SalesReport
  taxes: TaxReport
  ar: ArReport
}

// --- QBO push (P8) --------------------------------------------------------------
// Request params/bodies use the API's `property`/`date`/`month` aliases while
// responses emit `property_id`/`business_date` — that asymmetry is the backend
// contract (QboPushRequest field aliases in portal_api.py), mirrored on purpose.

export interface JeLine {
  gl_account_code: string
  account_name: string
  posting: 'Debit' | 'Credit'
  amount: string // always positive; direction is `posting`
  memo: string
}

export interface JePlan {
  property_id: string
  business_date: string
  lines: JeLine[]
  total_debits: string
  total_credits: string
  request_hash: string
}

/** Ledger rows record actual push attempts — `already-pushed` is a push
 * OUTCOME (nothing new recorded), so it never appears here. */
export type PushLedgerStatus = 'pushed' | 'failed' | 'stale'

export interface PushLedgerRow {
  push_id: number
  property_id: string
  business_date: string
  request_hash: string
  qbo_je_id: string | null
  status: PushLedgerStatus
  message: string | null
  pushed_at: string // ISO datetime with offset — safe for `new Date()`
}

/** `stale` and `failed` arrive as HTTP 200 — consumers must branch on
 * `status`, never treat a resolved push call as success. */
export type PushResultStatus = 'pushed' | 'already-pushed' | 'stale' | 'failed'

export interface PushResult {
  status: PushResultStatus
  qbo_je_id: string | null
  message: string | null
}

/** One entry of the structured 422 `detail: {"unmapped": [...]}` the QBO
 * preview/push endpoints return when GL curation is incomplete. */
export interface UnmappedGlLine {
  major: string
  sub_category: string
  line_item: string
}

// --- POST /ingest result (see server.py's ingest handler) --------------------

export interface IngestResult {
  pms_source: string
  report_type: string
  property_id: string
  business_date: string
  staged: number
  mapped: number
  unmapped: number
  skipped: number
}

// --- Employees / onboarding (A2.3) ------------------------------------------

export type EmploymentStatus = 'active' | 'inactive' | 'leave' | 'terminated'

export interface Employee {
  employee_id: number
  property_id: string
  department_id: number | null
  full_name: string
  pay_type: string
  // D2: GM-maintained scheduling aid ("can't work Tuesdays") — operational,
  // never money/medical. Written via PUT /api/schedule/employees/{id}/
  // availability-note; travels on the roster the builder already loads.
  availability_note: string | null
  // E3 classification/compliance — metadata, never money. Written via
  // PATCH /api/employees/{id}; `terminated` only via the terminate action.
  // The compliance trio (i9/w4/completeness) is null for callers below the
  // onboarder/payroll tier — need-to-know, not an absence of data.
  employment_status: EmploymentStatus
  full_part_time: 'full_time' | 'part_time' | null
  i9_submitted_on: string | null
  w4_submitted_on: string | null
  payroll_data_complete: boolean | null
}

/** One person's worked hours and estimated cost over a window, from the same
 *  promoted labor facts Schedule 14 is built on. */
export interface EmployeeWork {
  employee_id: number
  hours: string
  ot_hours: string
  /** null = WITHHELD, not zero. Cost over hours is the person's effective pay
   *  rate, so the server serves it only to the roles that may see a rate
   *  (payroll_admin / org_admin / property_gm). A department manager gets real
   *  hours beside a null — they run the schedule, they do not price it. */
  est_cost: string | null
}

export interface EmployeeUpdate {
  full_name?: string
  pay_type?: string
  department_id?: number
  effective_from?: string
}

// --- Labor analytics (payroll dashboard) -------------------------------------
// Mirrors LaborAnalyticsModel in src/usali/portal_api.py. Every money figure is
// a DEPARTMENT aggregate carrying the statement's own suppression.

export interface LaborDay {
  business_date: string
  hours: string
  ot_hours: string
  /** Disclosed departments only, decided per day — see reporting._discloses. */
  est_cost: string
  rooms_occupied: string | null
  revenue: string | null
  /** Hours per department on this day, keyed by department name. HOURS ONLY:
   *  a per-day per-department cost would re-derive a solo employee's rate. */
  department_hours: Record<string, string>
}

export interface LaborDepartment {
  department: string
  hours: string
  ot_hours: string
  /** null = suppressed: fewer than two priced employees on a day carrying cost. */
  est_cost: string | null
  target_hours: string | null
}

export interface LaborAnalytics {
  property_id: string
  date_from: string
  date_to: string
  days: LaborDay[]
  departments: LaborDepartment[]
  hours_total: string
  ot_hours_total: string
  cost_total: string
  revenue_total: string
  rooms_total: string
  fte: string | null
  suppressed_departments: number
  unpriced_hours: string
}

/** The hourly rate on an employee's primary placement. Decimal as a string —
 *  money never becomes a float. payroll_admin-gated and audited on both sides. */
export interface PayRate {
  employee_id: number
  pay_rate: string | null
}

export interface SetPayRateBody {
  pay_rate: string
  /** Rates are effective-dated: a new rate opens from this day and the previous
   *  one closes, so days already worked keep costing what they cost. */
  effective_from?: string}

export interface Onboarded {
  employee_id: number
  keycloak_subject: string | null
  property_id: string
  full_name: string
}

export interface OnboardEmployeeRequest {
  full_name: string
  email: string | null
  property: string
  pay_type: string
  role: string | null
  department_id: number | null
}

export interface Me {
  subject: string
  username: string
  roles: string[]
}

// --- Payroll PII vault (Pillar C1) -------------------------------------------
// Mirrors PublicKeyModel / ProfileStatus / ProfileBody in src/usali/pii_api.py.

export interface PayrollPublicKey {
  key_id: string
  suite: string
  public_key: string // base64 SEC1 uncompressed P-256 point
}

// The vault has NO read path: status is booleans only, never a sealed value or
// plaintext. The deposit destination left this surface in E5 — see the
// deposit-accounts types below.
export interface PayrollProfileStatus {
  employee_id: number
  ssn_on_file: boolean
  tax_elections_on_file: boolean
}

// Each sealed field is a SealedEnvelope JSON string (see lib/piiSeal.ts). Only
// PROVIDED fields are written (blind overwrite).
export interface SealedProfileBody {
  ssn?: string
  tax_elections?: string
}

// --- Deposit accounts (E5) ---------------------------------------------------
// Mirrors DepositAccountBody / DepositAccountsStatus in src/usali/pii_api.py.
// The chain routes NET pay: amount/percent rows carve pieces off, and exactly
// one `remainder` row — always LAST — receives everything left. PUT replaces
// the WHOLE chain (full re-entry + re-seal, the C1 contract); ordinals are
// server-assigned from array position.

export type AllocationType = 'amount' | 'percent' | 'remainder'

export interface DepositAccountBody {
  allocation_type: AllocationType
  allocation_value: string | null // decimal string; null iff remainder
  account_type: 'checking' | 'savings'
  sealed_account: string // SealedEnvelope JSON, aad `${employeeId}:deposit:${ordinal}:account`
  sealed_routing: string // aad `${employeeId}:deposit:${ordinal}:routing`
}

export interface DepositAccountsBody {
  accounts: DepositAccountBody[]
}

export interface DepositAccountStatus {
  ordinal: number
  allocation_type: AllocationType
  allocation_value: string | null
  // null only on migration-backfilled rows whose pre-E5 profile never stated
  // a type — shown as unknown, fixed by re-entering the chain.
  account_type: string | null
  account_on_file: boolean
  routing_on_file: boolean
}

export interface DepositAccountsStatus {
  employee_id: number
  accounts: DepositAccountStatus[]
}

// --- Pay runs (Pillar C2) ------------------------------------------------------
// Mirrors PayRunCreateBody / PayRunSummary / DepartmentAggregate / PayRunDetail /
// FetchResultsModel in src/usali/payroll_run_api.py field-for-field. Money is
// department-AGGREGATE only — the API never returns per-employee amounts (those
// stay encrypted at rest for C3's payroll-admin detail endpoint).

export interface PayRunCreateBody {
  property: string
  in_period: string // ISO date; any date inside the target pay period
}

export interface PayRunSummary {
  pay_run_id: number
  property_id: string
  period_start: string
  period_end: string
  check_date: string
  status: string // "draft" | "submitted" | "processed" | "failed"
  provider: string
}

export interface DepartmentAggregate {
  department: string
  hours: string
  gross: string
  employer_burden: string
}

export interface PayRunDetail extends PayRunSummary {
  failure_reason: string | null
  department_aggregates: DepartmentAggregate[]
}

export interface FetchResultsResponse {
  status: string
  lines: number
}

// Mirrors PayRunLineModel / PayRunLinesModel in src/usali/payroll_run_api.py
// field-for-field (Pillar C3): the ONLY per-employee money read in the system.
// The server audits EVERY read of this endpoint, so the UI must request it only
// on an explicit user action — never as a side effect of navigation.

export interface PayRunLineDetail {
  employee_id: number
  employee_name: string
  hours: string
  gross: string
  employee_taxes: string
  employer_taxes: string
  net: string
}

export interface PayRunLines {
  pay_run_id: number
  status: string
  lines: PayRunLineDetail[]
}

// --- Kiosk time capture (B1) -------------------------------------------------

// Mirrors DeviceModel / EnrolledModel in kiosk.py (operator-side admin).
export interface KioskDeviceInfo {
  device_id: number
  property_id: string
  name: string
  revoked: boolean
}

export interface EnrolledKiosk {
  device_id: number
  property_id: string
  name: string
  token: string // plaintext — returned exactly once, at enrollment
}

export interface KioskEmployee {
  employee_id: number
  full_name: string
}

/** A roster tile. `state` is on the ROSTER response only — search deliberately
 *  omits it, so a typed name never discloses whether that person is working. */
export interface KioskRosterEmployee extends KioskEmployee {
  state: 'out' | 'in' | 'on_break'
}

export type PunchType = 'clock_in' | 'lunch_start' | 'lunch_end' | 'clock_out'

/** Mirrors PunchStateModel in src/usali/kiosk.py. `allowed` is the punch
 *  order rule as the server will enforce it — the kiosk offers exactly these
 *  and never guesses at the sequence itself. */
export interface KioskPunchState {
  employee_id: number
  state: 'out' | 'in' | 'on_break'
  allowed: PunchType[]
}

export interface Punched {
  punch_id: number
  employee_id: number
  punch_type: PunchType
  business_date: string
}

// Kiosk my-week (D2): mirrors MyWeekShiftModel/MyWeekModel in
// src/usali/kiosk.py — published schedule only, the tapped employee's OWN
// shifts only, hours-and-times only (never money).
export interface KioskWeekShift {
  business_date: string // ISO YYYY-MM-DD
  department: string // name only — the kiosk shows words, not ids
  start_time: string // "HH:MM"
  end_time: string // "HH:MM"
  crosses_midnight: boolean
}

export interface KioskMyWeek {
  employee_id: number
  week_start: string
  published: boolean // false = no PUBLISHED schedule for this week (drafts invisible)
  shifts: KioskWeekShift[]
}

// --- Face-first kiosk (Pillar F) ---------------------------------------------
// Mirrors KioskConfigModel/IdentifyResultModel in src/usali/kiosk.py.

export interface KioskConfig {
  // True only when matching is enabled AND this device's state has an
  // encoded biometric posture — the kiosk picks camera-first vs roster on it.
  matching_enabled: boolean
}

export interface KioskIdentifyResult {
  // no_template is collapsed into no_match server-side, and the raw score
  // never leaves the server — the device population reads neither
  // enrollment posture nor how close a probe got (F8 disclosure lens).
  state: 'matched' | 'no_match' | 'no_face'
  employee_id?: number | null
  full_name?: string | null
}

// F7: enrollment status for the Employees page — mirrors
// FaceTemplateStatusModel in src/usali/face_enrollment.py. The embedding has
// NO read path; status is everything this surface ever sees.
export interface FaceTemplateStatus {
  employee_id: number
  enrolled: boolean
  model_version?: string | null
  notice_version?: string | null
  created_at?: string | null
}

// Mirrors FaceTemplateModel — the 201 body from enrollment.
export interface FaceTemplateEnrolled {
  employee_id: number
  model_version: string
  notice_version: string | null
}

// --- Scheduling (Pillar D1) --------------------------------------------------
// Mirrors TemplateBody/TemplateModel/WeekBody/ShiftBody/ShiftModel/WeekModel
// and the Projection* models in src/usali/schedule_api.py field-for-field.
// Conventions carried from the backend: request bodies use the `property`
// alias while responses emit `property_id` (the P8 asymmetry, on purpose);
// times serialize as zero-padded "HH:MM" strings (no seconds); dates as ISO
// `YYYY-MM-DD`; hours/cost decimals as exact strings.

export interface TemplateCreateBody {
  property: string
  department_id: number
  name: string
  start_time: string // "HH:MM"
  end_time: string // "HH:MM"
  crosses_midnight: boolean
}

export interface ShiftTemplate {
  template_id: number
  property_id: string
  department_id: number
  name: string
  start_time: string // "HH:MM"
  end_time: string // "HH:MM"
  crosses_midnight: boolean
}

export interface ScheduleWeekCreateBody {
  property: string
  week_start: string // ISO date; MUST be on the payroll Monday grid — the
  // server is authoritative and 422s off-grid dates (rendered verbatim).
}

export interface ShiftCreateBody {
  business_date: string
  department_id: number
  start_time: string // "HH:MM"
  end_time: string // "HH:MM"
  crosses_midnight: boolean
  employee_id: number | null // null = OPEN shift (planned, unassigned)
  template_id: number | null // provenance only
}

export interface ScheduleShift {
  shift_id: number
  schedule_id: number
  business_date: string
  department_id: number
  start_time: string // "HH:MM"
  end_time: string // "HH:MM"
  crosses_midnight: boolean
  employee_id: number | null
  template_id: number | null
}

export interface ScheduleWeek {
  schedule_id: number
  property_id: string
  week_start: string
  status: string // "draft" | "published"
  version: number // 0 until first publish; bumped on each publish
  published_at: string | null // ISO timestamp of the latest publish; null on draft
  shifts: ScheduleShift[]
}

// THE MONEY DISCIPLINE (B3/C2/C3, carried to schedules): per-employee fields
// are HOURS ONLY; money exists solely as department aggregates with the
// fewer-than-two-assigned-employees suppression (est_cost null) and a
// complementary total. Never add a per-employee money field here.

export interface ScheduleProjectionEmployee {
  employee_id: number
  full_name: string
  total_hours: string
  regular_hours: string
  ot_hours: string
}

export interface ScheduleProjectionWarning {
  code: string // "scheduled_overtime" | "clopening" | "seventh_day"
  employee_id: number
  full_name: string
  business_date: string
  hours: string // OT hours for scheduled_overtime; "0.00" otherwise — never money
}

export interface ScheduleProjectionDepartment {
  department: string
  hours: string
  est_cost: string | null // null = suppressed (< 2 distinct assigned employees)
}

export interface ScheduleProjection {
  schedule_id: number
  week_start: string
  // D3: last day whose PUNCHED hours were merged into the projection
  // ("Includes actual hours through …"); null when the week is not current.
  merged_through: string | null
  employees: ScheduleProjectionEmployee[]
  warnings: ScheduleProjectionWarning[]
  departments: ScheduleProjectionDepartment[]
  total_est_cost: string // sums ONLY non-suppressed departments (complementary)
  suppressed_departments: number
  unpriced_hours: string // assigned hours carrying no cost (exempt or rate-less)
}

// --- Scheduling targets / forecast / standards (Pillar D2) -------------------
// Mirrors StandardBody/StandardModel, ForecastDayBody/ForecastBody/
// ForecastDayModel/ForecastSavedModel, TargetDayModel/TargetDepartmentModel/
// TargetsModel and AvailabilityNoteModel in src/usali/schedule_api.py
// field-for-field. THE MONEY RULE: targets are HOURS-only — a target cost
// would need an average department rate, which for a small priced population
// IS an individual's rate. No money-shaped field may ever appear here.

export type StandardBasis = 'fixed_hours_per_day' | 'minutes_per_occupied_room'

export interface StandardUpsertBody {
  property: string
  department_id: number
  basis: StandardBasis
  value: number // hours or minutes — never money
}

export interface LaborStandard {
  standard_id: number
  property_id: string
  department_id: number
  basis: string
  value: string // "16.00" — the numeric-string convention, 2dp
}

export interface ForecastDay {
  business_date: string
  occupied_rooms: number | null // null = the GM has not forecast this day
  // History hints from our own promoted ROOMS_OCCUPIED facts. Hints inform,
  // never dictate — and they NEVER auto-fill the input.
  hint_same_day_last_week: number | null
  hint_trailing_avg: number | null
}

export interface ForecastUpsertBody {
  property: string
  days: { business_date: string; occupied_rooms: number }[]
}

export interface ForecastSaved {
  property_id: string
  saved: number
}

export interface TargetDay {
  business_date: string
  // null = no standard for the department, or a per-room day with no
  // forecast — null, NOT zero: absence of a forecast is not zero demand.
  target_hours: string | null
  scheduled_hours: string
}

export interface TargetDepartment {
  department: string
  days: TargetDay[]
  target_total: string | null // sums the NON-null days; null when every day is
  scheduled_total: string
  days_without_forecast: number
}

export interface ScheduleTargets {
  schedule_id: number
  week_start: string
  departments: TargetDepartment[]
}

export interface AvailabilityNote {
  employee_id: number
  availability_note: string | null
}

// --- CRM demand (Pillar J) ---------------------------------------------------
// Mirrors DemandSurfaceModel/DemandDayModel/DemandCapabilitiesModel in
// src/usali/crm_api.py field-for-field. Demand INFORMS the forecast, never
// becomes it: nothing here is a target and nothing auto-fills an input. A
// null figure means "this provider does not speak that dimension" — render
// an absent chip part, NEVER a 0 (the D2 forecast-null rule). `labels`
// (block/event names — a wedding's name is somebody's name) are scheduler-
// surface working data: this page only, never the kiosk.

export interface DemandCapabilities {
  rooms_on_books: boolean
  group_rooms: boolean
  event_covers: boolean
}

export interface DemandDay {
  stay_date: string
  rooms_on_books: number | null
  group_rooms: number | null
  event_covers: number | null
  labels: string
  pulled_at: string // the as-of stamp — say WHEN a figure is from
}

export interface DemandSurface {
  configured: boolean // false = no CRM provider configured; render nothing
  provider: string | null
  capabilities: DemandCapabilities
  days: DemandDay[]
}

// --- Schedule adherence (Pillar D3) ------------------------------------------
// Mirrors AdherenceDayModel/AdherenceDepartmentModel/AdherenceExceptionModel/
// AdherenceModel in src/usali/schedule_api.py field-for-field. ELAPSED days
// only (strictly before as_of — the future has nothing to adhere to); punched
// hours come from B2's merged timeline, so manager-corrected days are clean.
// THE MONEY RULE: every employee-level row is HOURS ONLY — no money-shaped
// field may ever appear here.

export interface AdherenceDay {
  business_date: string
  scheduled_hours: string
  punched_hours: string
}

export interface AdherenceDepartment {
  department: string
  days: AdherenceDay[]
  scheduled_total: string
  punched_total: string
}

export type AdherenceExceptionCode = 'no_show' | 'unscheduled_punch' | 'deviation'

export interface AdherenceException {
  code: AdherenceExceptionCode
  employee_id: number
  full_name: string
  business_date: string
  scheduled_hours: string
  punched_hours: string
}

export interface ScheduleAdherence {
  schedule_id: number
  week_start: string
  as_of: string
  departments: AdherenceDepartment[]
  exceptions: AdherenceException[]
}

// --- Timecards (B2) ----------------------------------------------------------
// Mirrors DayModel / TimecardModel / TimecardSummaryModel in timecard_api.py.

export interface TimecardDayPunch {
  punch_id: number
  punch_type: string
  punched_at: string
  // False for purged AND never-stored alike: the manager-facing meaning
  // ("no evidence to show") is the same.
  has_photo: boolean
  // F6: server-side verification verdict from punch time. verified=green,
  // unverified=red (gates approval until acknowledged), no_template=grey
  // (cold start), null=recorded before/without matching.
  match_state: 'verified' | 'unverified' | 'no_template' | null
  match_score: number | null
}

export interface TimecardDay {
  business_date: string
  worked_minutes: number
  warnings: string[]
  punches: TimecardDayPunch[]
}

export interface TimecardSummary {
  timecard_id: number
  employee_id: number
  employee_name: string
  period_start: string
  status: string
  total_minutes: number
}

export interface Timecard extends TimecardSummary {
  period_end: string
  days: TimecardDay[]
}

// --- Property configuration: room inventory & fiscal calendar (issue #8) ----
// Mirrors InventoryRow/OooRow/FiscalConfigModel/ConfigResponse/PeriodRow in
// src/usali/property_config_api.py field-for-field.

export interface InventoryRow {
  inventory_id: number
  effective_date: string
  total_rooms: number
}

export interface OooRow {
  ooo_id: number
  start_date: string
  end_date: string
  room_count: number
  reason_code: string
  note: string | null
}

export interface FiscalConfig {
  calendar_type: 'calendar_month' | '445'
  fiscal_year_start_month: number
  week_start_weekday: number | null
}

export interface PropertyConfig {
  property_id: string
  inventory: InventoryRow[]
  out_of_order: OooRow[]
  fiscal_calendar: FiscalConfig | null
}

export interface FiscalPeriod {
  key: string
  start: string
  end: string
}

export const OOO_REASONS = [
  'maintenance',
  'renovation',
  'damage',
  'deep_clean',
  'other',
  'do_not_rent',
  'owner_occupied',
] as const

// --- Core performance statistics (issue #9) ---------------------------------
// Mirrors CoreMetricsModel/ReconLineModel/TrendPairModel/RollingStatModel/
// TrendsModel/PerformanceResponse in src/usali/portal_api.py field-for-field.
// Every decimal is an exact string (never a float); a `string | null` figure is
// WITHHELD (e.g. occupancy with no rooms available), not zero. The ADR basis is
// the property's stat-config choice, echoed on every metrics block.

export type AdrRoomBasis = 'as_reported' | 'exclude_comp_house'

export interface CoreMetrics {
  start: string
  end: string
  rooms_available: string
  rooms_sold: string
  adr_rooms_sold: string
  room_revenue: string
  total_revenue: string
  occupancy: string | null
  adr: string | null
  revpar: string | null
  trevpar: string | null
  adr_room_basis: string
}

/** One line of the PMS-KPI reconciliation: our computed value vs the ingested
 *  statistic, and whether they agree. Any field is null when its side is absent. */
export interface ReconLine {
  computed: string | null
  ingested: string | null
  agrees: boolean | null
}

/** A current-vs-prior trend basis (WoW / same-day-of-week). */
export interface TrendPair {
  current: string | null
  prior: string | null
  delta_pct: string | null
}

/** A 30-day rolling stat: mean, standard deviation, and the sample size `n`. */
export interface RollingStat {
  avg: string | null
  stdev: string | null
  n: number
}

export interface Trends {
  anchor: string
  wow: Record<string, TrendPair>
  mtd: Record<string, string | null>
  rolling_30: Record<string, RollingStat>
  dow: Record<string, TrendPair>
}

/** Labor productivity (issue #9): labor hours and cost per occupied room over
 *  the window. Mirrors LaborProductivityModel in portal_api.py. Hours are pure
 *  operational detail (never withheld); the cost fields are null and
 *  `cost_suppressed` is true when any contributing day withheld its labor cost
 *  (single-employee rate-derivation guard) — a WITHHELD figure, not zero. */
export interface LaborProductivity {
  labor_hours: string | null
  rooms_sold: string | null
  hours_per_occupied_room: string | null
  labor_cost: string | null
  cost_per_occupied_room: string | null
  cost_suppressed: boolean
}

export interface PerformanceResponse {
  property_id: string
  adr_room_basis: string
  // Echoes the fiscal period key when the `period=YYYY-Pnn` form was used;
  // null for an explicit `from`+`to` range.
  period: string | null
  start: string
  end: string
  current: CoreMetrics
  // null when there is no comparable prior window (e.g. no earlier data).
  prior_period: CoreMetrics | null
  prior_year: CoreMetrics | null
  prior_period_delta_pct: Record<string, string | null>
  prior_year_delta_pct: Record<string, string | null>
  reconciliation: Record<string, ReconLine>
  trends: Trends
  // Labor hours/cost per occupied room; cost fields withheld when suppressed.
  labor: LaborProductivity
  // Window length minus its data-complete days.
  days_excluded: number
}

// --- Public preview (anonymous front door, Part 1 backend) ------------------
// Mirrors the POST /api/preview response models. Anonymous, unauthenticated —
// no property_id, no persistence: a single PDF in, a preview payload out.

export interface PreviewPnlLine {
  major: string
  sub: string
  line_item: string
  amount: string
}

export interface PreviewPayload {
  pms_source: string
  report_type: string
  business_date: string
  pnl_lines: PreviewPnlLine[]
  kpis: { label: string; value: string }[]
  codes_recognized: number
  codes_mapped: number
  codes_needs_review: number
  net_total: string
}

export type PreviewResponse =
  | { status: 'ok'; payload: PreviewPayload }
  | { status: 'unsupported'; vendor: string; reason: string }
  | { status: 'unreadable'; hints: string[] }
