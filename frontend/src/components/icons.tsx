// Line icons for the sidebar nav (stroke-based, 24px grid, drawn to match the
// text weight). All are decorative — parents carry the accessible name — so
// every icon is aria-hidden.

import type { ComponentPropsWithoutRef } from 'react'

type IconProps = ComponentPropsWithoutRef<'svg'>

function Icon({ children, ...rest }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={18}
      height={18}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  )
}

export const StatementIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 3h9l4 4v14H6z" />
    <path d="M15 3v4h4M9.5 12h5M9.5 16h5" />
  </Icon>
)

export const CoverageIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3l7 3v5c0 4.4-2.9 7.6-7 9-4.1-1.4-7-4.6-7-9V6z" />
    <path d="M9 11.5l2 2 4-4.5" />
  </Icon>
)

export const UploadIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 15V4m0 0L8 8m4-4l4 4" />
    <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
  </Icon>
)

export const ReportsIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 20h16" />
    <path d="M7 20v-6m5 6V9m5 11v-9" />
  </Icon>
)

export const SyncIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20 11a8 8 0 0 0-14.9-3M4 13a8 8 0 0 0 14.9 3" />
    <path d="M4 4v4h4M20 20v-4h-4" />
  </Icon>
)

export const PeopleIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="9" cy="8.5" r="3.5" />
    <path d="M3.5 19.5c.6-3 2.8-4.5 5.5-4.5s4.9 1.5 5.5 4.5" />
    <path d="M15.5 5.6a3.5 3.5 0 0 1 0 5.8M17.5 15.3c1.6.6 2.7 1.9 3 4.2" />
  </Icon>
)

export const ClockIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </Icon>
)

export const KioskIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="5" y="3" width="14" height="18" rx="2" />
    <path d="M10 18h4" />
  </Icon>
)

export const CalendarIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="4" y="5" width="16" height="15" rx="2" />
    <path d="M4 9.5h16M8.5 3v4M15.5 3v4" />
  </Icon>
)

export const BanknoteIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="7" width="18" height="10" rx="2" />
    <circle cx="12" cy="12" r="2.5" />
    <path d="M6.5 10.5v3M17.5 10.5v3" />
  </Icon>
)

export const UserIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="8" r="3.5" />
    <path d="M5.5 20c.8-3.4 3.3-5 6.5-5s5.7 1.6 6.5 5" />
  </Icon>
)

export const WalletIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 7a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v1" />
    <rect x="4" y="8" width="17" height="11" rx="2" />
    <path d="M16.5 13.5h.01" strokeWidth={2.4} />
  </Icon>
)

export const MoonIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z" />
  </Icon>
)

export const FileIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 3h9l4 4v14H6z" />
    <path d="M15 3v4h4" />
  </Icon>
)

export const BankIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3.5 9L12 4l8.5 5v1h-17z" />
    <path d="M5.5 10v7M9.8 10v7M14.2 10v7M18.5 10v7M4 20h16" />
  </Icon>
)

export const GridIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="4" y="4" width="7" height="7" rx="1.2" />
    <rect x="13" y="4" width="7" height="7" rx="1.2" />
    <rect x="4" y="13" width="7" height="7" rx="1.2" />
    <rect x="13" y="13" width="7" height="7" rx="1.2" />
  </Icon>
)

export const SearchIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="M20 20l-4.4-4.4" />
  </Icon>
)

export const MenuIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </Icon>
)

export const CloseIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 6l12 12M18 6L6 18" />
  </Icon>
)

export const CollapseIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M11 7l-5 5 5 5M18 7l-5 5 5 5" />
  </Icon>
)

export const ExpandIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 7l5 5-5 5M13 7l5 5-5 5" />
  </Icon>
)

export const GaugeIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 15.5a8 8 0 1 1 16 0" />
    <path d="M12 15.5l3.6-4.6" />
    <circle cx="12" cy="15.5" r="1.4" fill="currentColor" stroke="none" />
  </Icon>
)

export const LogoutIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M14 4H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h7" />
    <path d="M10 12h10m0 0l-3.5-3.5M20 12l-3.5 3.5" />
  </Icon>
)

// --- row actions -------------------------------------------------------------

export const PencilIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z" />
    <path d="M14.5 6.5l3 3" />
  </Icon>
)

export const PauseIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M10 9.5v5M14 9.5v5" />
  </Icon>
)

export const PlayIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M10.5 9l5 3-5 3z" />
  </Icon>
)

export const UserMinusIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="9.5" cy="8" r="3.5" />
    <path d="M3.5 19.5a6 6 0 0 1 12 0" />
    <path d="M16.5 10.5h4.5" />
  </Icon>
)

export const TrendUpIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 16.5l5-5 3.5 3.5L20 7.5" />
    <path d="M15 7.5h5v5" />
  </Icon>
)

export const AlertIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 4.5l8 14.5H4z" />
    <path d="M12 10v4" />
    <circle cx="12" cy="16.6" r=".9" fill="currentColor" stroke="none" />
  </Icon>
)

export const ChecklistIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9 4.5H7a2 2 0 0 0-2 2V19a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6.5a2 2 0 0 0-2-2h-2" />
    <rect x="9" y="2.5" width="6" height="4" rx="1.2" />
    <path d="M8.75 13.5l2.25 2.25 4.25-5" />
  </Icon>
)
