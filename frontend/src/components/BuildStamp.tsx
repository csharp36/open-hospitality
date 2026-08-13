// A subtle, always-present build stamp: the short commit SHA baked into the
// SPA at image-build time (Dockerfile ARG -> Vite env), which is the SAME value
// the deploy tags the image with. It lives in the sidebar footer so every page
// a user can screenshot names its exact build — turning a vague bug report
// ("the Performance page was blank") into a specific commit to correlate
// against. Shows 'dev' locally (`npm run dev` passes no build arg).
export default function BuildStamp({ collapsed = false }: { collapsed?: boolean }) {
  const sha = import.meta.env.VITE_BUILD_SHA ?? 'dev'
  return (
    <p
      className={`mt-2 text-center text-[10px] leading-none text-ink-faint ${
        collapsed ? 'sr-only' : ''
      }`}
      title={`Build ${sha}`}
    >
      build {sha}
    </p>
  )
}
