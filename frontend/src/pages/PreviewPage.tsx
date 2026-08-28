// The public front door (/try). Written for a hotel owner who arrived from a
// link, has never heard of USALI, and is being asked to hand a PDF to a website
// they don't know yet.
//
// Three rules the copy on this page follows, because a stranger cannot check
// any of them for themselves:
//
//  1. Say what it can't do, on the page, unprompted. The preview reads ONE
//     report from Opera or AutoClerk. A SkyTouch pack dropped here comes back
//     "unreadable" (server.py `_PREVIEW_ADAPTERS`), so this page never invites
//     one, however much the signup form now offers SkyTouch.
//  2. No jargon that only makes sense from inside. "USALI" is the standard the
//     mapping follows; to an owner it is an acronym that explains nothing, so
//     the page describes what it DOES — sorts charges the way a hotel
//     accountant would — and leaves the acronym out.
//  3. Every control does something. Nothing disabled with "coming soon".

import { useState } from 'react'
import { ApiError, postPreview } from '../api/client'
import type { PreviewResponse } from '../api/types'
import { login } from '../auth/oidc'
import DropZone from './preview/DropZone'
import EdgeState from './preview/EdgeState'
import PreviewResult from './preview/PreviewResult'

type State =
  | { kind: 'idle' }
  | { kind: 'working' }
  | { kind: 'result'; res: PreviewResponse }
  | { kind: 'error'; message: string }

// Synthetic stand-ins built by scripts/gen_preview_samples.py and served from
// the SPA's own origin. They mirror only the layout of a real export — the real
// ones under docs/reference/samples carry production figures and can never be
// handed out. tests/test_preview_samples.py pins that these still parse.
const SAMPLES = [
  { label: 'an Opera trial balance', file: 'opera-trial-balance-sample.pdf' },
  { label: 'an AutoClerk transaction summary', file: 'autoclerk-transaction-summary-sample.pdf' },
] as const

export default function PreviewPage() {
  const [state, setState] = useState<State>({ kind: 'idle' })

  async function run(file: File) {
    setState({ kind: 'working' })
    try {
      setState({ kind: 'result', res: await postPreview(file) })
    } catch (e) {
      const message =
        e instanceof ApiError && e.status === 413 ? 'That file is too large (max 10 MB).'
        : e instanceof ApiError && e.status === 429 ? 'A lot of previews right now — try again shortly.'
        : e instanceof ApiError && e.status === 415 ? 'Please upload a PDF.'
        : 'Something went wrong reading that file. Please try again.'
      setState({ kind: 'error', message })
    }
  }

  // A sample runs through the SAME path as a dropped file — fetched from the
  // SPA's origin, then posted to /api/preview like any other upload. Nothing is
  // pre-baked, so what a visitor sees here is what the parser actually does.
  async function runSample(file: string) {
    setState({ kind: 'working' })
    try {
      const res = await fetch(`/samples/${file}`)
      if (!res.ok) throw new Error(`sample fetch failed: ${res.status}`)
      const blob = await res.blob()
      await run(new File([blob], file, { type: 'application/pdf' }))
    } catch {
      setState({ kind: 'error', message: 'That sample didn’t load. Please try again.' })
    }
  }

  const atStart = state.kind === 'idle' || state.kind === 'error'

  return (
    <main className="min-h-screen bg-brand-canvas text-brand-ink font-sans">
      <header className="flex items-center justify-between px-6 py-4 border-b border-brand-line">
        <span className="font-display text-lg">Open Hospitality</span>
        {/* Labelled for who it is FOR. Plain "Log in" sent first-time visitors
            to a Keycloak screen they have no account for and no way past — the
            page's own dead end, even though the button itself worked. */}
        <button type="button" onClick={() => { login().catch(() => {}) }}
          className="rounded-control border border-brand-accent px-3 py-1 text-sm text-brand-accent">
          Customer log in
        </button>
      </header>

      <div className="mx-auto max-w-2xl px-6 py-12 space-y-6">
        <div>
          <h1 className="font-display text-3xl leading-tight">
            See last night’s numbers as a P&amp;L.
          </h1>
          <p className="mt-3 text-brand-ink-muted">
            Drop in the night-audit report your front desk already prints. We read it, sort
            every charge the way a hotel accountant would, and show you the result —
            <span className="text-brand-ink"> with no account, and without keeping your
            file.</span>
          </p>
        </div>

        {atStart && <DropZone onFile={run} />}

        {atStart && (
          <div className="text-sm text-brand-ink-muted">
            <span>Don’t have one to hand? Try it on </span>
            {SAMPLES.map((s, i) => (
              <span key={s.file}>
                {i > 0 && <span> or </span>}
                <button
                  type="button"
                  onClick={() => { void runSample(s.file) }}
                  className="underline text-brand-accent"
                >
                  {s.label}
                </button>
              </span>
            ))}
            <span> — invented figures, real parsing.</span>
          </div>
        )}

        {state.kind === 'error' && <p role="alert" className="text-sm text-danger-red">{state.message}</p>}
        {state.kind === 'working' && <p className="text-brand-ink-muted">Reading your report…</p>}
        {state.kind === 'result' && state.res.status === 'ok' && <PreviewResult payload={state.res.payload} />}
        {state.kind === 'result' && state.res.status !== 'ok' &&
          <EdgeState res={state.res} onRetry={() => setState({ kind: 'idle' })} />}

        {atStart && <HowItWorks />}
      </div>
    </main>
  )
}

// Shown only before a preview runs: once there is a result on screen, that
// result is the explanation and this would just push it down the page.
function HowItWorks() {
  return (
    <section aria-label="How this works" className="space-y-4 border-t border-brand-line pt-6">
      <div>
        <h2 className="font-display text-lg">It reads the report you already have</h2>
        <p className="mt-1 text-sm text-brand-ink-muted">
          Nothing to install, and no connection to your property management system. The PDF
          your night audit prints is enough.
        </p>
      </div>
      <div>
        <h2 className="font-display text-lg">It sorts charges the way accountants do</h2>
        <p className="mt-1 text-sm text-brand-ink-muted">
          Room revenue apart from the shop and the parking; the taxes you are only
          collecting kept out of your income; and how guests actually paid shown
          separately from what they were charged. That is the split your accountant
          rebuilds by hand every month.
        </p>
      </div>
      <div>
        <h2 className="font-display text-lg">It tells you what it wasn’t sure about</h2>
        <p className="mt-1 text-sm text-brand-ink-muted">
          Every property has house codes nobody else uses. Rather than guess at one and
          quietly file it in the wrong place, the result tells you how many are still
          waiting on a person — this preview counts them; a workspace is where you settle
          them.
        </p>
      </div>
      <div>
        <h2 className="font-display text-lg">What it can’t do yet</h2>
        <p className="mt-1 text-sm text-brand-ink-muted">
          This preview reads one report at a time, and only from Opera or AutoClerk. If you
          run SkyTouch, or you want the whole audit pack read at once, that works inside a
          workspace — but not on this page, and it will tell you so rather than guess.
        </p>
      </div>
      <p className="text-sm text-brand-ink-muted">
        Your file is read in memory and thrown away. It is never written to disk, never
        stored, and nothing about it survives this page.
      </p>
    </section>
  )
}
