// A subtle, always-present build stamp: the short commit SHA baked into the
// SPA at image-build time (Dockerfile ARG -> Vite env), which is the SAME value
// the deploy tags the image with. It lives in the sidebar footer so every page
// a user can screenshot names its exact build — turning a vague bug report
// ("the Performance page was blank") into a specific commit to correlate
// against. Shows 'dev' locally (`npm run dev` passes no build arg).
//
// Clicking it opens the release notes for that build (see ReleaseNotes).

import { useState } from 'react'

import ReleaseNotes from './ReleaseNotes'

export default function BuildStamp({ collapsed = false }: { collapsed?: boolean }) {
  const sha = import.meta.env.VITE_BUILD_SHA ?? 'dev'
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={`Build ${sha} — view release notes`}
        aria-label={`Build ${sha}. View release notes`}
        className={`mt-2 w-full text-center text-[10px] leading-none text-ink-faint hover:text-ink-muted ${
          collapsed ? 'sr-only' : ''
        }`}
      >
        build {sha}
      </button>
      {open && <ReleaseNotes buildSha={sha} onClose={() => setOpen(false)} />}
    </>
  )
}
