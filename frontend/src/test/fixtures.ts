// Hand-built SosReport fixture mirroring the P6 render tests and the real
// HISJ/OPERA 2026-07-07 shape: 4dp storage-scale decimal strings, 10866.37
// total operating revenue, negative settlements, prior-year statistics.

import type { User } from 'oidc-client-ts'
import type {
  CoverageReport,
  CpaPack,
  Employee,
  JePlan,
  PropertyInfo,
  PushLedgerRow,
  SosReport,
  StagedTxn,
} from '../api/types'
import type { AuthContextValue } from '../auth/authContext'

// Synchronous authed context for router/page tests: the app now nests every
// page under <RequireAuth>, so tests supply an already-authenticated context
// via <AuthContext.Provider> to render the guarded UI deterministically.
export const AUTHED_CONTEXT: AuthContextValue = {
  user: { expired: false, profile: { preferred_username: 'tester' } } as unknown as User,
  loading: false,
  logout: () => {},
}

export function makeSosReport(overrides: Partial<SosReport> = {}): SosReport {
  return {
    property_id: 'HISJ',
    pms_source: 'OPERA',
    business_date: '2026-07-07',
    date_from: null,
    date_to: null,
    operated_departments: [
      {
        sub_category: 'Rooms',
        lines: [
          {
            major: 'Operating Revenue',
            sub_category: 'Rooms',
            line_item: 'Transient Rooms Revenue',
            total: '10456.3700',
          },
        ],
        total: '10456.3700',
      },
    ],
    misc_income: [
      {
        major: 'Operating Revenue',
        sub_category: 'Miscellaneous Income',
        line_item: 'Parking',
        total: '410.0000',
      },
    ],
    misc_income_total: '410.0000',
    total_operating_revenue: '10866.3700',
    taxes: [
      {
        major: 'Pass-Through',
        sub_category: 'Taxes',
        line_item: 'Occupancy Tax',
        total: '1254.7600',
      },
    ],
    taxes_total: '1254.7600',
    settlements: [
      {
        major: 'Settlements',
        sub_category: 'Settlements',
        line_item: 'Visa/Mastercard',
        total: '-16.2000',
      },
    ],
    settlements_total: '-16.2000',
    other: [],
    other_total: '0.0000',
    rooms_segments: [
      { segment: 'Transient', rooms: '30', room_revenue: '7842.2775' },
      { segment: 'Group', rooms: '10', room_revenue: '2614.0925' },
    ],
    statistics: [
      {
        metric_code: 'ROOMS_OCCUPIED',
        day: '40.0000',
        mtd: '250.0000',
        ytd: '1200.0000',
        day_prior: '38.0000',
        mtd_prior: '241.0000',
        ytd_prior: '1150.0000',
      },
    ],
    // No promoted labor by default — labor sections stay hidden unless a test
    // overrides these (mirrors the backend's empty-labor SOS shape).
    payroll_expense: [],
    payroll_expense_total: '0',
    labor_hours_total: '0',
    labor_ot_hours_total: '0',
    labor_fte: null,
    labor_unpriced_hours: '0',
    labor_suppressed_departments: 0,
    // No processed pay run by default — the C3 variance section stays hidden
    // unless a test overrides this (mirrors the backend's null block).
    labor_variance: null,
    ...overrides,
  }
}

export const HISJ_PROPERTY: PropertyInfo = {
  property_id: 'HISJ',
  pms_source: 'OPERA',
  first_date: '2026-07-01',
  last_date: '2026-07-07',
  name: 'HOLIDAY INN & SUITES SAN JOSE',
}

export const SSSJ_PROPERTY: PropertyInfo = {
  property_id: 'SSSJ',
  pms_source: 'AUTOCLERK',
  first_date: '2026-07-01',
  last_date: '2026-07-07',
  name: 'SURESTAY PLUS BY BW',
}

/**
 * One-source coverage report exercising every card section: partial financial
 * mapping with missing codes and a needs-review worklist, one unmapped
 * segment code, and two unmapped (informational) statistics labels. Count
 * keys appear in the backend's semantic order (reporting.py _ordered_counts:
 * HIGH/MEDIUM/LOW, reviewed/needs-review/unreviewed) — the UI renders them
 * in insertion order, unsorted.
 */
export function makeCoverageReport(overrides: Partial<CoverageReport> = {}): CoverageReport {
  return {
    sources: [
      {
        pms_source: 'OPERA',
        financial: {
          dictionary_entries: 42,
          by_confidence: { HIGH: 30, MEDIUM: 8, LOW: 4 },
          by_review_status: { reviewed: 33, 'needs-review': 2, unreviewed: 7 },
          needs_review: [
            { code: '5105', line_item: 'Parking', notes: 'Ambiguous mapping' },
            { code: '9999', line_item: 'Misc Adjustments', notes: null },
          ],
          staged_codes: 40,
          mapped_codes: 38,
          missing_codes: ['1234', '5678'],
          exception_count: 3,
          gl_mapped: 41,
          gl_unmapped_codes: ['5007'],
        },
        segments: {
          dictionary_entries: 12,
          needs_review: [{ code: 'GRP-X', line_item: 'Group Other', notes: 'verify block code' }],
          staged_codes: 10,
          mapped_codes: 9,
          unmapped_codes: ['WALKIN'],
        },
        statistics: {
          dictionary_entries: 8,
          staged_labels: 8,
          mapped_labels: 6,
          unmapped_labels: ['House Use Rooms', 'Comp Rooms'],
        },
      },
    ],
    ...overrides,
  }
}

/**
 * CPA monthly pack for HISJ 2026-07, consistent with the SOS fixture shapes:
 * 4dp storage-scale strings, 10866.37 total operating revenue.
 */
export function makeCpaPack(overrides: Partial<CpaPack> = {}): CpaPack {
  return {
    property_id: 'HISJ',
    pms_source: 'OPERA',
    month: '2026-07',
    sales: {
      lines: [
        {
          major: 'Operating Revenue',
          sub_category: 'Rooms',
          line_item: 'Transient Rooms Revenue',
          mtd_amount: '10456.3700',
          day_count: 7,
        },
        {
          major: 'Operating Revenue',
          sub_category: 'Miscellaneous Income',
          line_item: 'Parking',
          mtd_amount: '410.0000',
          day_count: 3,
        },
      ],
      total_operating_revenue: '10866.3700',
    },
    taxes: {
      lines: [{ line_item: 'Occupancy Tax', gl_account_code: '2210', mtd_amount: '1254.7600' }],
      taxes_total: '1254.7600',
      room_revenue_base: '10456.3700',
    },
    ar: {
      balances: [
        {
          ledger_code: 'CITY',
          ledger_name: 'City Ledger',
          opening_balance: '5000.0000',
          closing_balance: '6200.0000',
          movement: '1200.0000',
        },
      ],
    },
    ...overrides,
  }
}

/**
 * Balanced JE plan whose totals (12,439.66) match the P8 plan's confirm-dialog
 * example: one debit line offset by three credits.
 */
export function makeJePlan(overrides: Partial<JePlan> = {}): JePlan {
  return {
    property_id: 'HISJ',
    business_date: '2026-07-07',
    lines: [
      {
        gl_account_code: '1100',
        account_name: 'Guest Ledger',
        posting: 'Debit',
        amount: '12439.6600',
        memo: 'HISJ 2026-07-07 daily activity',
      },
      {
        gl_account_code: '4100',
        account_name: 'Rooms Revenue',
        posting: 'Credit',
        amount: '10456.3700',
        memo: 'HISJ 2026-07-07 rooms revenue',
      },
      {
        gl_account_code: '2210',
        account_name: 'Occupancy Tax Payable',
        posting: 'Credit',
        amount: '1254.7600',
        memo: 'HISJ 2026-07-07 occupancy tax',
      },
      {
        gl_account_code: '4500',
        account_name: 'Parking Revenue',
        posting: 'Credit',
        amount: '728.5300',
        memo: 'HISJ 2026-07-07 parking revenue',
      },
    ],
    total_debits: '12439.6600',
    total_credits: '12439.6600',
    request_hash: 'a1b2c3d4',
    ...overrides,
  }
}

export function makePushLedgerRow(overrides: Partial<PushLedgerRow> = {}): PushLedgerRow {
  return {
    push_id: 1,
    property_id: 'HISJ',
    business_date: '2026-07-07',
    request_hash: 'a1b2c3d4',
    qbo_je_id: '987',
    status: 'pushed',
    message: null,
    pushed_at: '2026-07-08T09:15:00+00:00',
    ...overrides,
  }
}

/** The structured 422 detail body /api/qbo/preview|push return for
 * incomplete GL curation — a worklist, not prose. */
export const UNMAPPED_GL_DETAIL = {
  unmapped: [
    {
      major: 'Operating Revenue',
      sub_category: 'Miscellaneous Income',
      line_item: 'Parking',
    },
    {
      major: 'Operating Revenue',
      sub_category: 'Miscellaneous Income',
      line_item: 'Vending',
    },
  ],
}

export function makeEmployee(over: Partial<Employee> = {}): Employee {
  return { employee_id: 1, property_id: 'HISJ', department_id: null,
    full_name: 'Gina M', pay_type: 'salary', availability_note: null,
    employment_status: 'active', full_part_time: null, i9_submitted_on: null,
    w4_submitted_on: null, payroll_data_complete: false, ...over }
}

/** Two staged rows that sum exactly to the Parking line total (410.0000). */
export const PARKING_TXNS: StagedTxn[] = [
  {
    stage_id: 1,
    pms_source: 'OPERA',
    business_date: '2026-07-07',
    pms_trx_code: '5105',
    pms_trx_desc: 'PARKING SELF',
    amount: '250.0000',
    source_file: 'opera-2026-07-07.pdf',
  },
  {
    stage_id: 2,
    pms_source: 'OPERA',
    business_date: '2026-07-07',
    pms_trx_code: '5105',
    pms_trx_desc: 'PARKING VALET',
    amount: '160.0000',
    source_file: 'opera-2026-07-07.pdf',
  },
]
