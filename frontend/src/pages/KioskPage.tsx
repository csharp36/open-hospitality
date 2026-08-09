import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  ApiError, getKioskConfig, getKioskMyWeek, getKioskPunchState, getKioskRoster,
  getKioskSearch, postKioskIdentify, postPunch,
} from '../api/client'
import type { KioskRosterEmployee, PunchType } from '../api/types'
import { errorMessage } from '../lib/errors'
import { addDays, dayName, upcomingWeekMonday } from '../lib/week'
import { Card, controlClass } from '../components/ui'

const TOKEN_KEY = 'usali.kiosk.token' // legacy single-location key, migrated below
const ENROLLMENTS_KEY = 'usali.kiosk.enrollments'

/**
 * One enrollment = one property's device token, labeled for the picker. A
 * shared tablet serving two hotels holds TWO enrollments: each token stays
 * scoped to its own property server-side (that confinement is what keeps this
 * kiosk from leaking the other hotel's roster), and the employee's first tap
 * picks the location.
 */
type Enrollment = { label: string; token: string }

function loadEnrollments(): Enrollment[] {
  try {
    const raw = localStorage.getItem(ENROLLMENTS_KEY)
    if (raw) {
      const parsed: unknown = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed.filter(
          (e): e is Enrollment =>
            !!e && typeof e === 'object'
            && typeof (e as Enrollment).label === 'string'
            && typeof (e as Enrollment).token === 'string',
        )
      }
    }
  } catch {
    // Unreadable store: fall through to the legacy key.
  }
  const legacy = localStorage.getItem(TOKEN_KEY)
  return legacy ? [{ label: 'This location', token: legacy }] : []
}

function saveEnrollments(list: Enrollment[]): void {
  localStorage.setItem(ENROLLMENTS_KEY, JSON.stringify(list))
}

const PUNCHES: { type: PunchType; label: string; done: string }[] = [
  { type: 'clock_in', label: 'Clock In', done: 'Clocked in' },
  { type: 'lunch_start', label: 'Start Lunch', done: 'Lunch started' },
  { type: 'lunch_end', label: 'End Lunch', done: 'Lunch ended' },
  { type: 'clock_out', label: 'Clock Out', done: 'Clocked out' },
]

// The kiosk runs full-screen on an iPad, so it asks for the system face
// first: on iPadOS that resolves to SF Pro and the whole surface stops
// looking like a web page in a browser. Scoped to this page only — the rest
// of the app keeps the product font.
const APPLE_FONT =
  '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", ' +
  'system-ui, "Segoe UI", sans-serif'

/**
 * The time, big, the way every clock-in device shows it.
 *
 * Ticks on a 1s interval and is cleared on unmount. It is also the kiosk's
 * proof of life: a frozen clock is the first thing anyone notices about a
 * tablet that has quietly lost its connection.
 */
function KioskClock({ ink, mutedInk }: { ink: string; mutedInk: string }) {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  return (
    <div className="flex flex-col">
      <span className={`text-[3.25rem] font-semibold leading-none tracking-tight tabular-nums ${ink}`}>
        {now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
      </span>
      <span className={`mt-1.5 text-sm font-medium ${mutedInk}`}>
        {now.toLocaleDateString([], {
          weekday: 'long', month: 'long', day: 'numeric',
        })}
      </span>
    </div>
  )
}

/**
 * Light or dark, chosen once and remembered on the device.
 *
 * A wall tablet in a lit corridor wants the light face; the same tablet in a
 * dim back office at 3am wants the dark one. That is a property of the ROOM,
 * not of the product, so it belongs to the device rather than to a build.
 */
type KioskTheme = 'light' | 'dark'
const THEME_KEY = 'usali.kiosk.theme'

function loadTheme(): KioskTheme {
  return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'
}

/**
 * The ambient field the whole surface sits on: three wide, heavily blurred
 * colour fields, over off-white or over near-black.
 *
 * Static, not animated. This screen is mounted for a whole shift on a battery
 * device, and a permanently animating full-screen blur is the kind of thing
 * that shows up as heat in someone's hand.
 */
function AmbientBackdrop({ theme }: { theme: KioskTheme }) {
  const light = theme === 'light'
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      <div
        className={`absolute -left-[15%] -top-[25%] size-[70vw] rounded-full bg-[#4f46e5] blur-[130px] ${
          light ? 'opacity-[0.16]' : 'opacity-30'
        }`}
      />
      <div
        className={`absolute -right-[20%] top-[10%] size-[60vw] rounded-full bg-[#0ea5e9] blur-[130px] ${
          light ? 'opacity-[0.14]' : 'opacity-[0.22]'
        }`}
      />
      <div
        className={`absolute -bottom-[30%] left-[25%] size-[65vw] rounded-full bg-[#a855f7] blur-[140px] ${
          light ? 'opacity-[0.13]' : 'opacity-25'
        }`}
      />
      {/* A fine grain over the top. Large flat gradients band badly on a
          retina panel; noise is the standard cure and costs one tiny SVG. */}
      <div
        className={`absolute inset-0 ${light ? 'opacity-[0.02]' : 'opacity-[0.035] mix-blend-overlay'}`}
        style={{
          backgroundImage:
            "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E\")",
        }}
      />
    </div>
  )
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .map((w) => w[0] ?? '')
    .slice(0, 2)
    .join('')
    .toUpperCase()
}

/**
 * A per-person avatar colour, stable for life.
 *
 * Keyed on employee_id, not on the name, so marrying and changing your
 * surname does not change the colour your muscle memory looks for. Every hue
 * is cool: the STATUS owns green, amber and red on this screen, and an avatar
 * that happened to be green would read as "on shift" from across the room.
 */
const AVATAR_HUES = [
  'linear-gradient(150deg,#6366f1,#4338ca)', // indigo
  'linear-gradient(150deg,#8b5cf6,#6d28d9)', // violet
  'linear-gradient(150deg,#3b82f6,#1d4ed8)', // blue
  'linear-gradient(150deg,#06b6d4,#0e7490)', // cyan
  'linear-gradient(150deg,#0ea5e9,#0369a1)', // sky
  'linear-gradient(150deg,#a855f7,#7e22ce)', // purple
  'linear-gradient(150deg,#14b8a6,#0f766e)', // teal
  'linear-gradient(150deg,#d946ef,#a21caf)', // fuchsia
]

function avatarHue(employeeId: number): string {
  return AVATAR_HUES[employeeId % AVATAR_HUES.length]!
}

/**
 * A roster tile: avatar, name, status pill, on a glass card.
 *
 * WEIGHTED, not uniform. An earlier pass gave every tile a heavy coloured ring
 * — which meant a roster where nobody had clocked in yet was a wall of red
 * alarm, and the one person actually on shift was no louder than the twenty
 * who were not. Now the card carries the state: on-shift tiles are lit (green
 * border, tinted surface, soft glow) and off-shift tiles go quiet. Red survives
 * where it is still useful — as a small pill on the individual tile — instead
 * of shouting twenty times at once.
 *
 * The status is a WORD as well as a colour, because a colour-blind employee has
 * to find their own tile as fast as anyone else and a kiosk is used in a hurry.
 */
const TILE_STATE = {
  dark: {
    in: {
      card: 'border-ok-green/45 bg-ok-green/[0.08] shadow-[0_0_36px_-14px_rgb(34_197_94/0.85)]',
      pill: 'bg-ok-green/20 text-ok-green',
      dot: 'bg-ok-green',
    },
    on_break: {
      card: 'border-warn-amber/45 bg-warn-amber/[0.08] shadow-[0_0_36px_-14px_rgb(245_158_11/0.8)]',
      pill: 'bg-warn-amber/20 text-warn-amber',
      dot: 'bg-warn-amber',
    },
    out: {
      card: 'border-white/[0.09] bg-white/[0.06]',
      pill: 'bg-white/[0.08] text-white/55',
      dot: 'bg-danger-red/80',
    },
    name: 'text-white',
    hover: 'hover:bg-white/[0.12]',
  },
  // The light face is built on WHITE cards with hairline borders and a real
  // drop shadow — on a light background a translucent card has nothing behind
  // it to tint, so the glass trick that carries the dark face reads as mud.
  light: {
    // The card SURFACE stays white for every state. Tinting it green over an
    // already-coloured backdrop turned the on-shift cards muddy — on a light
    // face the state belongs to the border, the glow and the pill, where it
    // stays clean against white.
    in: {
      card: 'border-ok-green/55 bg-white/85 shadow-[0_8px_24px_-10px_rgb(34_197_94/0.55)]',
      pill: 'bg-ok-green/15 text-ok-green',
      dot: 'bg-ok-green',
    },
    on_break: {
      card: 'border-warn-amber/60 bg-white/85 shadow-[0_8px_24px_-10px_rgb(245_158_11/0.55)]',
      pill: 'bg-warn-amber/15 text-warn-amber',
      dot: 'bg-warn-amber',
    },
    out: {
      card: 'border-slate-900/[0.07] bg-white/70 shadow-[0_4px_16px_-8px_rgb(15_23_42/0.28)]',
      pill: 'bg-slate-900/[0.05] text-slate-500',
      dot: 'bg-danger-red/80',
    },
    name: 'text-slate-900',
    hover: 'hover:bg-white/90',
  },
} as const

const STATE_TEXT = {
  in: 'On the clock',
  on_break: 'On lunch',
  out: 'Not clocked in',
} as const

function RosterTile({
  employee, onPick, theme,
}: {
  employee: KioskRosterEmployee
  onPick: () => void
  theme: KioskTheme
}) {
  const palette = TILE_STATE[theme]
  const tone = palette[employee.state] ?? palette.out
  const on = employee.state !== 'out'
  return (
    <button
      onClick={onPick}
      className={`group flex flex-col items-center gap-3 rounded-[26px] border px-4 py-5 backdrop-blur-2xl backdrop-saturate-150 transition-all duration-200 hover:-translate-y-1 active:scale-[0.97] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60 ${tone.card} ${palette.hover}`}
    >
      <span
        aria-hidden="true"
        className={`relative grid size-[4.5rem] place-items-center rounded-2xl text-xl font-bold tracking-wide text-white shadow-lg transition-opacity ${
          on ? '' : 'opacity-70 group-hover:opacity-100'
        }`}
        style={{ background: avatarHue(employee.employee_id) }}
      >
        {/* One soft highlight off the top-left, so the avatar reads as an
            object under a light rather than as a flat swatch. */}
        <span className="absolute inset-0 rounded-2xl bg-[radial-gradient(circle_at_30%_20%,rgba(255,255,255,0.38),transparent_60%)]" />
        <span className="relative">{initials(employee.full_name)}</span>
      </span>
      <span className="flex w-full flex-col items-center gap-1.5">
        <span className={`w-full truncate text-center text-sm font-semibold ${palette.name}`}>
          {employee.full_name}
        </span>
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ${tone.pill}`}
        >
          <span aria-hidden="true" className={`size-1.5 rounded-full ${tone.dot}`} />
          {STATE_TEXT[employee.state] ?? STATE_TEXT.out}
        </span>
      </span>
    </button>
  )
}

/** What the kiosk says you are doing right now. The server derives the same
 *  thing from your last punch and refuses anything else — this is the reading
 *  of it, so the screen never offers a button that would 409. */
const STATE_LABEL: Record<string, string> = {
  out: 'Not clocked in',
  in: 'On the clock',
  on_break: 'On lunch',
}

// What the identify miss screen says, per state. Every path leads to the same
// SEARCH — a failed match never blocks the punch (the wage rule), it just
// stops being face-first.
const MISS_MESSAGES: Record<string, string> = {
  no_match: 'No match found — search your name to clock in.',
  no_face: 'No face detected — try again, or search your name.',
  error: 'Face matching is unavailable — search your name to clock in.',
}

/** Enrollment: a location label + device token, entered ONCE per hotel. */
function Enroll({
  onSaved, onCancel,
}: { onSaved: (e: Enrollment) => void; onCancel?: () => void }) {
  const [label, setLabel] = useState('')
  const [value, setValue] = useState('')
  return (
    <Card>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          onSaved({ label, token: value })
        }}
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Location name</span>
          <input className={controlClass} value={label} aria-label="Location name"
            placeholder="Holiday Inn Express" onChange={(e) => setLabel(e.target.value)} required />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-xs font-medium text-ink-muted">Device token</span>
          <input className={controlClass} value={value} aria-label="Device token"
            onChange={(e) => setValue(e.target.value)} required />
        </label>
        <div className="flex gap-2">
          <button type="submit"
            className="rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast">
            Enroll this kiosk
          </button>
          {onCancel && (
            <button type="button" onClick={onCancel}
              className="rounded-control border border-line px-3 py-2 text-sm text-ink-muted">
              Cancel
            </button>
          )}
        </div>
      </form>
    </Card>
  )
}

export default function KioskPage() {
  const [enrollments, setEnrollments] = useState<Enrollment[]>(loadEnrollments)
  const [active, setActive] = useState<Enrollment | null>(null)
  const [adding, setAdding] = useState(false)
  const [selected, setSelected] = useState<{ id: number; name: string } | null>(null)
  // True when `selected` came from a face match — the confirm card greets and
  // offers "Not me"; a search-selected card is just the name.
  const [identified, setIdentified] = useState(false)
  const [searching, setSearching] = useState(false)
  const [missState, setMissState] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [showWeek, setShowWeek] = useState(false)
  const [done, setDone] = useState<string | null>(null)
  const [theme, setTheme] = useState<KioskTheme>(loadTheme)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  // A single enrollment needs no picker; with several, the employee's first
  // tap picks the location and `active` holds it until Switch.
  const current = active ?? (enrollments.length === 1 ? enrollments[0] ?? null : null)

  // Which flow this device runs (F5): camera-first when the server says the
  // flag is on AND the property's state has an encoded posture. Until the
  // answer arrives nothing renders below — the ROSTER MUST NOT flash on a
  // face-first kiosk (it exists to keep names off the idle screen).
  const config = useQuery({
    queryKey: ['kiosk-config', current?.token],
    queryFn: () => getKioskConfig(current!.token),
    enabled: current !== null,
  })
  const matching = config.data?.matching_enabled === true
  // Config unreachable (NOT a dead token — those drop the enrollment below):
  // the flow choice is unknowable, but the search + punch endpoints may be
  // perfectly healthy. Search is the one fallback that works in both flows,
  // so it renders — a kiosk that shows nothing is a blocked punch (F8).
  const configDown = config.isError && current !== null

  const roster = useQuery({
    queryKey: ['kiosk-roster', current?.token],
    queryFn: () => getKioskRoster(current!.token),
    // Never fetched in face-first mode — the whole point is that the names
    // are not on this screen (or the wire).
    enabled: current !== null && config.data !== undefined && !matching,
  })

  const needle = query.trim()
  const search = useQuery({
    queryKey: ['kiosk-search', current?.token, needle],
    queryFn: () => getKioskSearch(current!.token, needle),
    enabled: current !== null && ((matching && searching) || configDown)
      && needle.length >= 3,
  })

  // "My week" (D2): the tapped employee's shifts from the latest PUBLISHED
  // schedule for the UPCOMING week — this week's Monday + 7 days, computed
  // client-side (the server validates the grid and confines the answer to the
  // device's property and the tapped employee).
  const weekStart = upcomingWeekMonday(new Date())
  const myWeek = useQuery({
    queryKey: ['kiosk-my-week', current?.token, selected?.id, weekStart],
    queryFn: () => getKioskMyWeek(current!.token, selected!.id, weekStart),
    enabled: current !== null && selected !== null && showWeek,
  })

  // What this employee may punch next. Fetched the moment they are identified
  // and re-read after every punch, so the bar always offers exactly the
  // actions the server would accept. Its absence never blocks the punch — the
  // server is the gate, this only saves the employee a refusal.
  const punchState = useQuery({
    queryKey: ['kiosk-punch-state', current?.token, selected?.id],
    queryFn: () => getKioskPunchState(current!.token, selected!.id),
    enabled: current !== null && selected !== null,
  })
  const allowed = punchState.data?.allowed
  const offered = allowed === undefined
    ? PUNCHES
    : PUNCHES.filter((p) => allowed.includes(p.type))

  // Only on the roster screen: the face-first flow never lists anyone, so
  // there is no population to count without disclosing one.
  const onShift =
    !matching && !selected && roster.data
      ? roster.data.filter((e) => e.state !== 'out').length
      : null

  // Live camera. Face-first mode streams on every screen but the week view
  // (idle capture, search, confirm all punch from it); roster mode only once
  // an employee is selected. The punch photo is evidence reviewed at
  // approval; in face-first mode the server also verifies it 1:1 against the
  // punched employee's template.
  const cameraOn =
    current !== null && !showWeek && (matching || selected !== null)

  // Header ink: over a live camera feed it is always light-on-dark, whatever
  // the theme — dark text on an arbitrary video frame is unreadable, and the
  // scrim behind it is what makes the white legible.
  const chrome = cameraOn || theme === 'dark'
    ? {
        ink: 'text-white',
        mutedInk: 'text-white/55',
        chip: 'border-white/10 bg-white/10 text-white',
      }
    : {
        ink: 'text-slate-900',
        mutedInk: 'text-slate-500',
        chip: 'border-slate-900/[0.08] bg-white/70 text-slate-700 shadow-sm',
      }

  useEffect(() => {
    if (!cameraOn) return
    let stream: MediaStream | null = null
    navigator.mediaDevices
      .getUserMedia({ video: true })
      .then((s) => {
        stream = s
        if (videoRef.current) videoRef.current.srcObject = s
      })
      .catch(() => {})
    return () => stream?.getTracks().forEach((t) => t.stop())
  }, [cameraOn])

  async function capture(): Promise<Blob> {
    const canvas = canvasRef.current
    const video = videoRef.current
    if (canvas && video) {
      canvas.width = video.videoWidth || 320
      canvas.height = video.videoHeight || 240
      canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
    }
    return new Promise<Blob>((resolve) => {
      canvas?.toBlob((b) => resolve(b ?? new Blob()), 'image/jpeg', 0.8)
    })
  }

  function resetToIdle() {
    setSelected(null)
    setIdentified(false)
    setSearching(false)
    setMissState(null)
    setQuery('')
    setShowWeek(false)
  }

  const identify = useMutation({
    mutationFn: async () => {
      const photo = await capture()
      return postKioskIdentify(current!.token, photo)
    },
    onSuccess: (r) => {
      if (r.state === 'matched' && r.employee_id != null && r.full_name != null) {
        setSelected({ id: r.employee_id, name: r.full_name })
        setIdentified(true)
      } else {
        setMissState(r.state)
        setSearching(true)
      }
    },
    onError: () => {
      // Identify down ≠ punch down: fall back to search (the 401/403 case is
      // handled by the dead-token watch below, which unmounts all of this).
      setMissState('error')
      setSearching(true)
    },
  })

  const punch = useMutation({
    mutationFn: async (type: PunchType) => {
      const photo = await capture()
      return postPunch(current!.token, selected!.id, type, photo)
    },
    onSuccess: (_r, type) => {
      const entry = PUNCHES.find((p) => p.type === type)
      setDone(`${entry?.done ?? 'Punched'} — ${selected?.name}`)
      resetToIdle()
      setTimeout(() => setDone(null), 4000)
    },
    // The next employee at this kiosk must not inherit the last one's state.
    onSettled: () => void punchState.refetch(),
  })

  function addEnrollment(e: Enrollment) {
    const next = [...enrollments, e]
    setEnrollments(next)
    saveEnrollments(next)
    setActive(e)
    setAdding(false)
  }

  // A rejected/revoked token has no login to redirect to — drop THAT
  // enrollment only (the other hotel's token is still good) and fall back to
  // the picker or, with nothing left, the enrollment form. Watches every
  // token-bearing call: a device can be revoked mid-punch, not just at load.
  const deadTokenError = [
    roster.error, punch.error, myWeek.error, config.error, identify.error,
  ].find((e) => e instanceof ApiError && (e.status === 401 || e.status === 403))
  useEffect(() => {
    if (deadTokenError && current) {
      const next = enrollments.filter((e) => e.token !== current.token)
      setEnrollments(next)
      saveEnrollments(next)
      // The migration path would resurrect a dead legacy token — clear it too.
      if (localStorage.getItem(TOKEN_KEY) === current.token) {
        localStorage.removeItem(TOKEN_KEY)
      }
      setActive(null)
      resetToIdle()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deadTokenError])

  if (enrollments.length === 0 || adding) {
    return (
      <div className="p-6">
        <Enroll
          onSaved={addEnrollment}
          onCancel={enrollments.length > 0 ? () => setAdding(false) : undefined}
        />
      </div>
    )
  }

  if (current === null) {
    return (
      <div className="flex flex-col gap-4 p-6">
        <h1 className="text-xl font-semibold text-ink">Where are you working today?</h1>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {enrollments.map((e) => (
            <button key={e.token}
              onClick={() => setActive(e)}
              className="rounded-card border border-line bg-surface-raised p-6 text-lg text-ink">
              {e.label}
            </button>
          ))}
        </div>
        <button onClick={() => setAdding(true)}
          className="self-start rounded-control border border-line px-3 py-2 text-sm text-ink-muted">
          Add a location
        </button>
      </div>
    )
  }

  return (
    // A kiosk is a dedicated device, not a page in an app. It runs full-screen
    // on an iPad for a whole shift, so: the system face, safe-area insets so
    // nothing hides under the home indicator or a rounded corner, and every
    // control in thumb reach of someone standing at a wall mount.
    <div
      className={`fixed inset-0 flex flex-col overflow-hidden [-webkit-tap-highlight-color:transparent] ${
        theme === 'light' ? 'bg-[#f2f4f9]' : 'bg-[#07080f]'
      }`}
      style={{ fontFamily: APPLE_FONT }}
    >
      <AmbientBackdrop theme={theme} />
      {cameraOn && (
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="absolute inset-0 size-full object-cover"
        />
      )}
      <canvas ref={canvasRef} className="hidden" />
      {/* Scrims top and bottom: text over a camera feed has no fixed
          background, so it needs its own contrast rather than luck. Skipped
          when there is no feed — over the ambient field they would only mute
          it. */}
      {cameraOn && (
        <>
          <div className="pointer-events-none absolute inset-x-0 top-0 h-56 bg-gradient-to-b from-black/85 via-black/45 to-transparent" />
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-2/3 bg-gradient-to-t from-black/85 via-black/55 to-transparent" />
        </>
      )}

      <header
        className="relative flex items-start justify-between gap-4 px-10"
        style={{
          paddingTop: 'max(2rem, env(safe-area-inset-top))',
          paddingLeft: 'max(2.5rem, env(safe-area-inset-left))',
          paddingRight: 'max(2.5rem, env(safe-area-inset-right))',
        }}
      >
        <div className="flex flex-col gap-2">
          <KioskClock ink={chrome.ink} mutedInk={chrome.mutedInk} />
          <h1 className={`text-sm font-medium uppercase tracking-[0.16em] ${chrome.mutedInk}`}>
            {current.label}
          </h1>
        </div>
        <div className="flex items-center gap-2.5">
          {onShift !== null && (
            <span className={`rounded-full border px-4 py-2 text-sm font-medium backdrop-blur-xl ${chrome.chip}`}>
              {onShift} on the clock
            </span>
          )}
          {selected && (
            <span className={`rounded-full border px-4 py-2 text-sm font-medium backdrop-blur-xl ${chrome.chip}`}>
              {STATE_LABEL[punchState.data?.state ?? ''] ?? 'Checking …'}
            </span>
          )}
          {enrollments.length > 1 && (
            <button
              onClick={() => { setActive(null); resetToIdle() }}
              className={`rounded-full border px-4 py-2 text-sm backdrop-blur-xl transition-colors ${chrome.chip}`}>
              Switch location
            </button>
          )}
          {/* Day/night is a property of the ROOM this tablet is mounted in, so
              it lives on the device and not in a build. */}
          <button
            onClick={() => {
              const next = theme === 'light' ? 'dark' : 'light'
              setTheme(next)
              localStorage.setItem(THEME_KEY, next)
            }}
            aria-label={theme === 'light' ? 'Switch to dark' : 'Switch to light'}
            className={`rounded-full border px-3.5 py-2 text-sm backdrop-blur-xl transition-colors ${chrome.chip}`}>
            {theme === 'light' ? '☾' : '☀'}
          </button>
        </div>
      </header>

      {/* Roster flow (matching off): the badge wall IS the screen. It centres
          in the space the camera would occupy rather than hugging the dock —
          with no camera running, a bottom-docked grid left the whole screen
          empty above it. */}
      {!matching && config.data !== undefined && !selected ? (
        // overflow-y-auto on the SCROLLER and m-auto on the grid: the wall
        // centres when it fits and scrolls when it does not. `justify-center`
        // on a scroller centres the overflow too, which clips the first and
        // last rows out of reach.
        <div className="relative flex min-h-0 flex-1 flex-col overflow-y-auto px-8 py-6">
          {roster.isPending && <p className={`text-center ${chrome.mutedInk}`}>Loading roster …</p>}
          {roster.isError && (
            <p className="text-center text-danger-red">
              Roster failed: {errorMessage(roster.error)}
            </p>
          )}
          {roster.data && roster.data.length > 0 && (
            <div className="m-auto grid w-full max-w-6xl grid-cols-[repeat(auto-fill,minmax(9.5rem,1fr))] gap-4">
              {roster.data.map((e) => (
                <RosterTile
                  key={e.employee_id}
                  employee={e}
                  theme={theme}
                  onPick={() => setSelected({ id: e.employee_id, name: e.full_name })}
                />
              ))}
            </div>
          )}
          {roster.data && roster.data.length === 0 && (
            <p className={`text-center ${chrome.mutedInk}`}>Nobody is assigned to this property yet.</p>
          )}
        </div>
      ) : (
        <div className="relative flex-1" />
      )}

      {/* The dock: everything the employee touches lives here. */}
      <div
        className="relative flex flex-col items-center gap-4 px-8"
        style={{ paddingBottom: 'max(2.5rem, calc(env(safe-area-inset-bottom) + 1.5rem))' }}
      >
        {done && (
          <p className="rounded-full bg-ok-green px-5 py-2.5 text-base font-semibold text-white">
            {done}
          </p>
        )}
        {punch.isError && (
          <p className="rounded-full bg-danger-red px-5 py-2.5 text-base font-semibold text-white">
            {errorMessage(punch.error)}
          </p>
        )}

        {/* Face-first flow (matching on): camera idle screen, no roster grid. */}
        {matching && !selected && !searching && (
          <>
            <p className="text-base text-white/80">
              Step up to the camera and tap — we&apos;ll find your timecard.
            </p>
            <button
              disabled={identify.isPending}
              onClick={() => identify.mutate()}
              aria-label="Tap to clock in or out"
              className="w-full max-w-2xl rounded-[28px] bg-accent px-8 py-7 text-2xl font-semibold text-accent-contrast shadow-[0_16px_44px_-16px_rgb(79_70_229/0.9)] transition-transform active:scale-[0.98] disabled:opacity-50">
              {identify.isPending ? 'Looking …' : 'Tap to clock in or out'}
            </button>
          </>
        )}

        {/* Search fallback: self-identification, never a browsable list.
            Also the whole kiosk when /config itself is down. */}
        {!selected && ((matching && searching) || configDown) && (
          <div className="w-full max-w-2xl rounded-[26px] border border-white/10 bg-white/[0.08] p-6 shadow-[0_20px_60px_-20px_rgb(0_0_0/0.8)] backdrop-blur-2xl backdrop-saturate-150">
            {configDown && !missState && (
              <p className="mb-3 text-sm text-ink">{MISS_MESSAGES['error']}</p>
            )}
            {missState && (
              <p className="mb-3 text-sm text-ink">
                {MISS_MESSAGES[missState] ?? MISS_MESSAGES['error']}
              </p>
            )}
            <input
              className={`${controlClass} h-14 w-full px-4 text-lg`}
              aria-label="Search your name"
              placeholder="Type at least 3 letters of your name"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
            <div className="mt-3 flex max-h-64 flex-col gap-2 overflow-y-auto">
              {search.data?.map((e) => (
                <button key={e.employee_id}
                  onClick={() => { setSelected({ id: e.employee_id, name: e.full_name }); setIdentified(false) }}
                  className="rounded-card border border-line bg-surface p-4 text-left text-lg text-ink">
                  {e.full_name}
                </button>
              ))}
              {search.data && search.data.length === 0 && needle.length >= 3 && (
                <p className="text-sm text-ink-muted">No one found — check the spelling.</p>
              )}
            </div>
            <button
              onClick={() => { if (configDown) void config.refetch(); else resetToIdle() }}
              className="mt-4 h-12 w-full rounded-control border border-line text-sm text-ink-muted">
              {configDown ? 'Try again' : 'Back to camera'}
            </button>
          </div>
        )}

        {selected && !showWeek && (
          <div className="flex w-full max-w-5xl flex-col items-center gap-5">
            <p className="text-2xl font-semibold text-white">
              {identified ? `Hi ${selected.name}` : selected.name}
            </p>
            {/* Only the punches the server would ACCEPT. You cannot start a
                lunch you have not clocked in for, and you cannot clock out
                while still on one — the button simply is not there, so the
                rule reads as guidance rather than as a refusal after the fact. */}
            <div className="flex w-full flex-wrap justify-center gap-4">
              {offered.map((p) => (
                <button key={p.type} disabled={punch.isPending}
                  onClick={() => punch.mutate(p.type)}
                  className="min-w-[13rem] flex-1 rounded-[26px] bg-accent px-6 py-8 text-xl font-semibold text-accent-contrast shadow-[0_16px_44px_-18px_rgb(79_70_229/0.95)] transition-transform active:scale-[0.98] disabled:opacity-50">
                  {p.label}
                </button>
              ))}
            </div>
            {allowed !== undefined && offered.length === 0 && (
              <p className="text-base text-white/80">
                Nothing to punch right now — see a manager.
              </p>
            )}
            <div className="flex flex-wrap justify-center gap-3">
              <button onClick={() => setShowWeek(true)}
                className="rounded-full border border-white/15 bg-white/[0.06] px-6 py-3 text-base font-medium text-white backdrop-blur-xl transition-colors hover:bg-white/[0.14]">
                My week
              </button>
              {identified && (
                <button
                  onClick={() => {
                    setSelected(null)
                    setIdentified(false)
                    setMissState('no_match')
                    setSearching(true)
                  }}
                  className="rounded-full border border-white/15 bg-white/[0.06] px-6 py-3 text-base text-white/80 backdrop-blur-xl transition-colors hover:bg-white/[0.14]">
                  Not me
                </button>
              )}
              <button onClick={resetToIdle}
                className="rounded-full border border-white/15 bg-white/[0.06] px-6 py-3 text-base text-white/80 backdrop-blur-xl transition-colors hover:bg-white/[0.14]">
                Cancel
              </button>
            </div>
          </div>
        )}

        {selected && showWeek && (
          <div className="w-full max-w-3xl rounded-[26px] border border-white/10 bg-white/[0.08] p-6 shadow-[0_20px_60px_-20px_rgb(0_0_0/0.8)] backdrop-blur-2xl backdrop-saturate-150">
          <p className="mb-3 text-lg text-ink">
            {selected.name} — week of {weekStart}
          </p>
          {myWeek.isPending && <p className="text-sm text-ink-muted">Loading week …</p>}
          {myWeek.isError && (
            <p className="text-sm text-danger-red">
              Week failed: {errorMessage(myWeek.error)}
            </p>
          )}
          {myWeek.data &&
            (myWeek.data.published ? (
              <ul className="divide-y divide-line">
                {Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)).map((day) => {
                  const shifts =
                    myWeek.data?.shifts.filter((s) => s.business_date === day) ?? []
                  return (
                    <li key={day} className="flex items-start gap-4 py-3 text-lg">
                      <span className="w-32 shrink-0 text-ink">
                        {dayName(day)} {day}
                      </span>
                      {shifts.length === 0 ? (
                        <span className="text-ink-muted">—</span>
                      ) : (
                        <span className="flex flex-col gap-1 text-ink">
                          {shifts.map((s, i) => (
                            <span key={i}>
                              {s.department} · {s.start_time}–{s.end_time}
                              {s.crosses_midnight ? ' (+1d)' : ''}
                            </span>
                          ))}
                        </span>
                      )}
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="py-3 text-lg text-ink-muted">No published schedule yet</p>
            ))}
          <button onClick={() => setShowWeek(false)}
            className="mt-4 h-12 w-full rounded-control border border-line text-sm text-ink-muted">
            Back
          </button>
          </div>
        )}
      </div>
    </div>
  )
}
