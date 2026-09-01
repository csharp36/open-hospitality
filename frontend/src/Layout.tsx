// App shell: collapsible left sidebar (brand, sectioned nav, user card) plus a
// top bar holding the mobile menu button and a search stub. Everything is
// token-based (surface/ink/accent/nav-active) so dark mode needs no variants,
// and the whole chrome is print:hidden — the wall-grid print view
// (SchedulePage) shows ONLY the week grid.
//
// Nav is grouped: Dashboard on top, then Setup, then Employee Management,
// then Accounting.
// `soon: true` items are visible-but-inert placeholders for pages that land
// later. Collapsed mode keeps every control's accessible name: labels go
// sr-only (never unmounted), so tests and screen readers see the same nav.
// The Setup link is the one nav entry whose accessible name carries data —
// match it with { name: /^Setup/ } unless you are pinning the count.

import { useState, type ComponentType, type SVGProps } from 'react'
import { Link, Outlet } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'

import { currentTheme, toggleTheme, type Theme } from './lib/theme'
import { GlobalPropertyProvider } from './lib/GlobalPropertyProvider'
import { useGlobalProperty } from './lib/propertyContext'
import { hasRole } from './lib/roles'
import { badgeLabel, useChecklist, type ChecklistBadge } from './lib/useChecklist'
import { useAuth } from './auth/authContext'
import { getMe } from './api/client'
import type { Me } from './api/types'
import BuildStamp from './components/BuildStamp'
import OrgPicker from './components/OrgPicker'
import { badgeToneClasses } from './lib/badgeTones'
import {
  BankIcon,
  BanknoteIcon,
  CalendarIcon,
  ChecklistIcon,
  ClockIcon,
  CloseIcon,
  CollapseIcon,
  CoverageIcon,
  ExpandIcon,
  FileIcon,
  GaugeIcon,
  GridIcon,
  KioskIcon,
  LogoutIcon,
  MenuIcon,
  MoonIcon,
  PeopleIcon,
  ReportsIcon,
  SearchIcon,
  StatementIcon,
  SyncIcon,
  UploadIcon,
  WalletIcon,
} from './components/icons'

type IconComponent = ComponentType<SVGProps<SVGSVGElement>>

const COLLAPSED_KEY = 'usali.sidebar-collapsed'

// The one nav entry that carries a badge, so the route is named once rather
// than spelled twice — the `SECTIONS` item and the render-time match.
const SETUP_PATH = '/setup'

// `show` gates by role exactly as the old top bar did — the link is a
// convenience; enforcement stays server-side. `soon` renders a muted,
// non-interactive placeholder, and does NOT exempt an item from `show`:
// showing someone a coming-soon entry for work their role will never do is a
// promise the product cannot keep.
type NavItem = {
  label: string
  icon: IconComponent
  to?: string
  exact?: boolean
  soon?: boolean
  show?: (me: Me | undefined) => boolean
}

type NavSection = {
  label: string | null
  items: NavItem[]
}

const isScheduler = (me: Me | undefined) => hasRole(me, 'org_admin', 'property_gm')
const isPayroll = (me: Me | undefined) => hasRole(me, 'payroll_admin')
// org_admin ONLY: the API is require_grants(ORG_ADMIN), stricter than
// isScheduler, and a nav entry is a promise about what this account can do.
const isOrgAdmin = (me: Me | undefined) => hasRole(me, 'org_admin')

const SECTIONS: NavSection[] = [
  {
    label: null,
    items: [{ to: '/dashboard', label: 'Dashboard', icon: GaugeIcon }],
  },
  // Ungrouped and second, above both sections: setup belongs to neither
  // Accounting nor Employee Management, and it never hides — the badge
  // retires at all_clear but the page stays the home for reconnecting an
  // integration later. No `show`: reading the checklist needs only the
  // router's operator gate; the dismiss controls inside are gated separately.
  {
    label: null,
    items: [{ to: SETUP_PATH, label: 'Setup', icon: ChecklistIcon }],
  },
  {
    label: 'Employee Management',
    items: [
      { to: '/payroll-dashboard', label: 'Payroll Dashboard', icon: GridIcon },
      { to: '/employees', label: 'Employees', icon: PeopleIcon, show: isScheduler },
      // The schedule page IS the weekly schedule — one entry, not a live route
      // beside a placeholder promising the same thing.
      { to: '/schedule', label: 'Weekly Schedule', icon: CalendarIcon, show: isScheduler },
      { to: '/payroll', label: 'Pay runs', icon: BanknoteIcon, show: isPayroll },
      // Payroll-gated even though it is only a placeholder: a nav entry is a
      // promise about what this account can do, and a GM will never be the one
      // setting compensation.
      { label: 'Payroll & Compensation', icon: WalletIcon, soon: true, show: isPayroll },
      { to: '/timecards', label: 'Timecards', icon: ClockIcon, show: isScheduler },
      { to: '/kiosk', label: 'Kiosk', icon: KioskIcon },
      { to: '/kiosk-devices', label: 'Kiosks', icon: KioskIcon, show: isScheduler },
      { to: '/property-config', label: 'Property config', icon: FileIcon, show: isScheduler },
    ],
  },
  {
    label: 'Accounting',
    items: [
      { to: '/sos', label: 'SOS', icon: StatementIcon, exact: true },
      { to: '/upload', label: 'Upload', icon: UploadIcon },
      { to: '/reports', label: 'Reports', icon: ReportsIcon },
      { to: '/performance', label: 'Performance', icon: GaugeIcon },
      { to: '/qbo', label: 'QBO', icon: SyncIcon },
      { to: '/integrations', label: 'Integrations', icon: SyncIcon, show: isOrgAdmin },
      { to: '/coverage', label: 'Coverage', icon: CoverageIcon },
      { label: 'Daily Reports', icon: FileIcon, soon: true },
      { label: 'Night Audit', icon: MoonIcon, soon: true },
      { label: 'Financial Reports', icon: BankIcon, soon: true },
    ],
  },
]

const ROLE_LABELS: [string, string][] = [
  ['org_admin', 'Org Admin'],
  ['property_gm', 'Property GM'],
  ['payroll_admin', 'Payroll Admin'],
  ['accountant', 'Accountant'],
  ['department_manager', 'Dept Manager'],
]

function primaryRoleLabel(me: Me | undefined): string | null {
  if (me === undefined) return null
  const found = ROLE_LABELS.find(([role]) => hasRole(me, role))
  return found === undefined ? null : found[1]
}

// The pill is a glyph: '!' is punctuation and most screen readers announce
// nothing for it at default verbosity, so on its own the one state
// `badgeLabel` exists to signal would reach a screen-reader user as a bare
// "Setup". `title` cannot stand in — on a span that already has text it is not
// the accessible name, is not reliably announced, and is unreachable on touch.
// So the sentence goes into the link's `aria-label` (see `setupLinkName`) and the
// pill is hidden from the tree as the duplicate it is.
function SetupBadge({ badge, collapsed }: { badge: ChecklistBadge; collapsed: boolean }) {
  return (
    <span
      data-testid="setup-badge"
      aria-hidden="true"
      title={badge.title}
      // Collapsed, the rail centres a single chip per row; an `ml-auto` pill
      // would eat the free space and drag the Setup icon off that centreline,
      // so it leaves the flow and sits over the chip's corner instead
      // (`itemBase` is already `relative`).
      className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold ${badgeToneClasses[badge.tone]} ${
        collapsed ? 'absolute right-1 top-0.5' : 'ml-auto'
      }`}
    >
      {badge.text}
    </span>
  )
}

/**
 * The Setup link's accessible name: the visible label first, so Label-in-Name
 * (WCAG 2.5.3) still holds and "click Setup" still works by voice.
 *
 * `aria-label` rather than an sr-only sibling carrying the same sentence, for
 * two reasons.
 *
 * sr-only text is real text: three suites mount this shell, and two of them
 * query the checklist wording with `getByText`, which does not care that a
 * node is visually hidden. Putting the words in the name only — never in the
 * document — keeps the sidebar out of their results.
 *
 * And the two variants do not even agree on the name. In a browser they do:
 * Tailwind's sr-only sets position:absolute, which blockifies computed
 * display, so the accname algorithm inserts a separator. In jsdom no
 * stylesheet loads, the span stays display:inline, and the sr-only variant
 * computes "Setup3 items still to set up". `aria-label` is the only variant
 * whose name is the same in the tests and in the product — which is the
 * promise the header of this file makes.
 */
function setupLinkName(label: string, badge: ChecklistBadge | null): string | undefined {
  // Colon, not a space: it gives a screen reader a prosodic break instead of
  // a run-on, and `label` still prefixes the name, so Label-in-Name holds.
  return badge === null ? undefined : `${label}: ${badge.title}`
}

function SidebarContent({
  me,
  theme,
  onToggleTheme,
  username,
  onLogout,
  onNavigate,
  collapsed = false,
  onToggleCollapse,
}: {
  me: Me | undefined
  theme: Theme
  onToggleTheme: () => void
  username: string | undefined
  onLogout: () => void
  onNavigate?: () => void
  collapsed?: boolean
  onToggleCollapse?: () => void
}) {
  const roleLabel = primaryRoleLabel(me)
  const initials = (username ?? '?').slice(0, 2).toUpperCase()
  // The mobile drawer mounts a second copy of this component while it is open;
  // TanStack dedupes on the shared key, so that is still one fetch.
  //
  // No badge until the first successful read: it is an ambient pointer and
  // cannot honestly report a number it does not have. A *later* background
  // failure keeps the last-known count rather than flickering the pointer off
  // — TanStack retains `data`, and that is deliberate. Either way the loud
  // failure belongs on /setup, which is where the operator went to find out.
  const checklist = useChecklist()
  const badge = checklist.data === undefined ? null : badgeLabel(checklist.data)

  const itemBase = `group relative flex w-full items-center gap-2.5 rounded-lg py-[7px] text-sm font-medium transition-colors ${
    collapsed ? 'justify-center px-2' : 'pl-2 pr-2.5'
  }`
  const chipBase = 'grid size-7 shrink-0 place-items-center rounded-md transition-colors'
  const navLink = `${itemBase} text-ink-muted hover:bg-surface-sunken hover:text-ink`
  const navLinkActive = {
    className: `${itemBase} bg-nav-active font-semibold text-nav-active-ink before:absolute before:-left-2 before:top-1/2 before:h-[18px] before:w-[3px] before:-translate-y-1/2 before:rounded-full before:bg-accent`,
  }
  const labelClass = collapsed ? 'sr-only' : 'min-w-0 flex-1 truncate text-left'

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        className={
          collapsed
            ? 'flex flex-col items-center gap-1 px-2 pb-2 pt-4'
            : 'flex items-start justify-between px-4 pb-2 pt-4'
        }
      >
        <div className={collapsed ? 'text-center' : ''}>
          <span className="block text-lg font-semibold leading-tight tracking-tight">
            <span aria-hidden="true" className="text-accent">
              ◆
            </span>
            <span className={collapsed ? 'sr-only' : ''}>
              {' '}
              <span className="text-accent">Open</span> Hospitality
            </span>
          </span>
          <span
            className={
              collapsed
                ? 'sr-only'
                : 'block pl-5 text-[11px] font-semibold uppercase tracking-widest text-ink-faint'
            }
          >
            Hospitality Ops
          </span>
        </div>
        {onToggleCollapse !== undefined && (
          <button
            type="button"
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-expanded={!collapsed}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            onClick={onToggleCollapse}
            className="rounded-lg p-1.5 text-ink-faint hover:bg-surface-sunken hover:text-ink"
          >
            {collapsed ? <ExpandIcon /> : <CollapseIcon />}
          </button>
        )}
      </div>

      <nav aria-label="Primary" className="min-h-0 flex-1 overflow-y-auto px-3 pb-2">
        {SECTIONS.map((section, si) => {
          const visible = section.items.filter((e) => e.show === undefined || e.show(me))
          if (visible.length === 0) return null
          return (
            <div key={section.label ?? `sec-${si}`}>
              {section.label !== null &&
                (collapsed ? (
                  <div aria-hidden="true" className="mx-1 my-3 border-t border-line" />
                ) : (
                  <p className="mb-1 mt-5 flex items-center gap-2 px-2 text-[10.5px] font-bold uppercase tracking-[0.14em] text-ink-faint">
                    <span
                      aria-hidden="true"
                      className="inline-block size-1.5 rounded-full bg-accent/70"
                    />
                    {section.label}
                    <span aria-hidden="true" className="h-px flex-1 bg-line" />
                  </p>
                ))}
              <div className="flex flex-col gap-0.5">
                {visible.map((e) => {
                  const IconGlyph = e.icon
                  if (e.soon === true || e.to === undefined) {
                    return (
                      <span
                        key={e.label}
                        aria-disabled="true"
                        title={collapsed ? `${e.label} (soon)` : undefined}
                        className={`${itemBase} cursor-default text-ink-faint`}
                      >
                        <span className={chipBase}>
                          <IconGlyph className="shrink-0" />
                        </span>
                        <span className={labelClass}>{e.label}</span>
                        {!collapsed && (
                          <span className="rounded-full bg-surface-sunken px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide">
                            Soon
                          </span>
                        )}
                      </span>
                    )
                  }
                  const isSetup = e.to === SETUP_PATH
                  return (
                    <Link
                      key={e.to}
                      to={e.to}
                      className={navLink}
                      activeProps={navLinkActive}
                      activeOptions={e.exact === true ? { exact: true } : undefined}
                      onClick={onNavigate}
                      title={collapsed ? e.label : undefined}
                      aria-label={isSetup ? setupLinkName(e.label, badge) : undefined}
                    >
                      <span className={`${chipBase} group-hover:text-accent`}>
                        <IconGlyph className="shrink-0" />
                      </span>
                      <span className={labelClass}>{e.label}</span>
                      {/* Deliberately NOT inside `labelClass`: collapsed, the
                          count is the only thing still pointing at setup. */}
                      {isSetup && badge !== null && (
                        <SetupBadge badge={badge} collapsed={collapsed} />
                      )}
                    </Link>
                  )
                })}
              </div>
            </div>
          )
        })}
      </nav>

      <div className="border-t border-line p-3">
        <div
          className={`flex items-center gap-3 rounded-lg border border-line bg-surface p-2.5 ${
            collapsed ? 'flex-col gap-2 p-2' : ''
          }`}
        >
          <span
            aria-hidden="true"
            title={collapsed ? username : undefined}
            className="grid size-9 shrink-0 place-items-center rounded-full bg-accent-soft text-sm font-bold text-accent-ink"
          >
            {initials}
          </span>
          <span className={collapsed ? 'sr-only' : 'min-w-0 flex-1'}>
            <span className="block truncate text-sm font-semibold text-ink">{username ?? '—'}</span>
            <span className="block truncate text-xs text-ink-muted">{roleLabel ?? 'Operator'}</span>
          </span>
          <button
            type="button"
            aria-label="Toggle dark mode"
            aria-pressed={theme === 'dark'}
            title="Toggle dark mode"
            onClick={onToggleTheme}
            className="rounded-full px-1.5 py-0.5 text-base leading-none text-ink-muted hover:bg-surface-sunken hover:text-ink"
          >
            {theme === 'dark' ? '☀' : '☾'}
          </button>
        </div>
        <button
          type="button"
          onClick={onLogout}
          aria-label="Sign out"
          title="Sign out"
          className={`${navLink} mt-1 justify-center`}
        >
          <LogoutIcon className="shrink-0" />
          <span className={collapsed ? 'sr-only' : ''}>Sign out</span>
        </button>
        <BuildStamp collapsed={collapsed} />
      </div>
    </div>
  )
}

function GlobalPropertySelect() {
  const { property, setProperty, properties } = useGlobalProperty()
  if (properties === undefined || properties.length === 0) return null
  return (
    <select
      aria-label="Active property"
      title="Property (applies across the app)"
      className="rounded-full border border-line bg-surface px-3 py-1.5 text-sm font-medium text-ink"
      value={property ?? ''}
      onChange={(e) => setProperty(e.target.value)}
    >
      {properties.map((p) => (
        <option key={`${p.property_id}|${p.pms_source}`} value={p.property_id}>
          {p.property_id} — {p.pms_source}
        </option>
      ))}
    </select>
  )
}

export default function Layout() {
  const [theme, setTheme] = useState<Theme>(currentTheme)
  const [menuOpen, setMenuOpen] = useState(false)
  const [collapsed, setCollapsed] = useState<boolean>(
    () => localStorage.getItem(COLLAPSED_KEY) === '1',
  )
  const { user, logout } = useAuth()
  const me = useQuery({ queryKey: ['me'], queryFn: getMe })
  const username = user?.profile.preferred_username

  function handleToggleCollapse() {
    setCollapsed((prev) => {
      const next = !prev
      localStorage.setItem(COLLAPSED_KEY, next ? '1' : '0')
      return next
    })
  }

  const sidebarProps = {
    me: me.data,
    theme,
    onToggleTheme: () => setTheme(toggleTheme()),
    username,
    onLogout: () => logout(),
  }

  return (
    <GlobalPropertyProvider>
    <div
      className={`min-h-screen transition-[grid-template-columns] duration-200 md:grid ${
        collapsed ? 'md:grid-cols-[4.75rem_minmax(0,1fr)]' : 'md:grid-cols-[16.5rem_minmax(0,1fr)]'
      }`}
    >
      <aside className="sticky top-0 hidden h-screen border-r border-line bg-surface-raised md:block print:hidden">
        <SidebarContent
          {...sidebarProps}
          collapsed={collapsed}
          onToggleCollapse={handleToggleCollapse}
        />
      </aside>

      {menuOpen && (
        <div className="fixed inset-0 z-40 md:hidden print:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="absolute inset-0 bg-scrim"
            onClick={() => setMenuOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-surface-raised shadow-overlay">
            <div className="flex justify-end p-2">
              <button
                type="button"
                aria-label="Close menu"
                onClick={() => setMenuOpen(false)}
                className="rounded-lg p-2 text-ink-muted hover:bg-surface-sunken hover:text-ink"
              >
                <CloseIcon />
              </button>
            </div>
            <div className="min-h-0 flex-1">
              <SidebarContent {...sidebarProps} onNavigate={() => setMenuOpen(false)} />
            </div>
          </div>
        </div>
      )}

      <div className="min-w-0">
        <header className="flex items-center gap-3 border-b border-line bg-surface-raised px-4 py-2.5 md:px-6 print:hidden">
          <button
            type="button"
            aria-label="Open menu"
            onClick={() => setMenuOpen(true)}
            className="rounded-lg p-2 text-ink-muted hover:bg-surface-sunken hover:text-ink md:hidden"
          >
            <MenuIcon />
          </button>
          {/* Search is a visual stub until a search API exists. */}
          <div className="relative max-w-md flex-1">
            <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-ink-faint">
              <SearchIcon />
            </span>
            <input
              type="search"
              disabled
              aria-label="Search"
              title="Search is coming soon"
              placeholder="Search staff, reports… (soon)"
              className="w-full rounded-full border border-line bg-surface py-1.5 pl-10 pr-4 text-sm text-ink placeholder:text-ink-faint disabled:cursor-not-allowed"
            />
          </div>
          {/* Active org then property — the two selectors that scope every
              page, outermost first: an org switch invalidates the property. */}
          <div className="ml-auto flex items-center gap-3">
            <OrgPicker />
            <GlobalPropertySelect />
            {/* Pages portal their contextual filters here. */}
            <div id="topbar-slot" className="flex items-center gap-3" />
          </div>
        </header>
        <main className="mx-auto w-full max-w-[96rem] px-6 py-6">
          <Outlet />
        </main>
      </div>
    </div>
    </GlobalPropertyProvider>
  )
}
