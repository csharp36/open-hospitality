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

  return (
    <main className="min-h-screen bg-brand-canvas text-brand-ink font-sans">
      <header className="flex items-center justify-between px-6 py-4 border-b border-brand-line">
        <span className="font-display text-lg">Open Hospitality</span>
        <button type="button" onClick={() => { login().catch(() => {}) }}
          className="rounded-control border border-brand-accent px-3 py-1 text-sm text-brand-accent">
          Log in
        </button>
      </header>

      <div className="mx-auto max-w-2xl px-6 py-12 space-y-6">
        <div>
          <h1 className="font-display text-3xl leading-tight">See your night audit as a real P&amp;L.</h1>
          <p className="mt-2 text-brand-ink-muted">
            Drop the report your PMS already emails you. Mapped and shown back in seconds —
            <span className="text-brand-ink"> no account, nothing saved.</span>
          </p>
        </div>

        {(state.kind === 'idle' || state.kind === 'error') && <DropZone onFile={run} />}
        {state.kind === 'error' && <p role="alert" className="text-sm text-danger-red">{state.message}</p>}
        {state.kind === 'working' && <p className="text-brand-ink-muted">Reading your report…</p>}
        {state.kind === 'result' && state.res.status === 'ok' && <PreviewResult payload={state.res.payload} />}
        {state.kind === 'result' && state.res.status !== 'ok' &&
          <EdgeState res={state.res} onRetry={() => setState({ kind: 'idle' })} />}
      </div>
    </main>
  )
}
