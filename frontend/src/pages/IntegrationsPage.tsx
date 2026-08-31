// Per-tenant integration config (OH-17). One query for the page; the cards
// below are presentational. The provider field lists come from the API — this
// file must never grow one of its own, or it becomes a second copy of
// PROVIDERS with nothing checking it (design doc, section 3).

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getRouteApi } from '@tanstack/react-router'
import { useEffect, useRef, useState } from 'react'

import { ApiError, connectIntegration, getAuthorizeUrl, getIntegrations } from '../api/client'
import type { Integration, IntegrationProvider } from '../api/types'
import { Card, PageHeader, controlClass } from '../components/ui'
import { errorMessage } from '../lib/errors'

// getRouteApi avoids the router.tsx <-> IntegrationsPage.tsx circular value
// import — see QboPage.tsx for the same pattern.
const route = getRouteApi('/integrations')

const TITLES: Record<string, string> = {
  payroll: 'Payroll',
  accounting: 'Accounting',
  demand_feed: 'Demand feed',
}

/** Renders whatever fields the spec named. It has no list of its own — that
 * is the point of serving the specs. `aria-label` rather than a wrapping
 * <label>, because the accessible name is what the tests query by:
 * 'renders an input per spec field and sends what it collected' in
 * IntegrationsPage.test.tsx is where that name is exercised. */
function ProviderForm({
  integration, spec, onDone,
}: {
  integration: string
  spec: IntegrationProvider
  onDone: () => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const connect = useMutation({
    mutationFn: () => connectIntegration(integration, { provider: spec.provider, ...values }),
    onSuccess: onDone,
  })
  return (
    <form
      className="mt-2 space-y-2"
      onSubmit={(e) => { e.preventDefault(); connect.mutate() }}
    >
      {spec.fields.map((field) => (
        <input
          key={field.name}
          aria-label={field.name}
          type={field.secret ? 'password' : 'text'}
          className={controlClass}
          value={values[field.name] ?? ''}
          onChange={(e) => setValues((v) => ({ ...v, [field.name]: e.target.value }))}
        />
      ))}
      <button type="submit" className={controlClass}>{`Connect ${spec.label}`}</button>
      {connect.error !== null && (
        <p className="text-sm text-danger-red">{errorMessage(connect.error)}</p>
      )}
    </form>
  )
}

function OauthConnect({ spec }: { spec: IntegrationProvider }) {
  // A top-level navigation. The authorize endpoint hands back a URL instead
  // of a 302 so that this, and not the fetch seam in api/client.ts, is what
  // leaves the origin — its docstring in src/usali/integrations_api.py is
  // where that reasoning lives.
  const start = useMutation({
    mutationFn: getAuthorizeUrl,
    onSuccess: (res) => { window.location.assign(res.url) },
  })
  return (
    <div className="mt-2 space-y-2">
      <button type="button" className={controlClass} onClick={() => start.mutate()}>
        {`Connect ${spec.label}`}
      </button>
      {start.error !== null && (
        <p className="text-sm text-danger-red">{errorMessage(start.error)}</p>
      )}
    </div>
  )
}

function IntegrationCard({
  item, onDone, note, error,
}: {
  item: Integration
  onDone: () => void
  note?: string
  error?: string
}) {
  const title = TITLES[item.integration] ?? item.integration
  const connected = item.providers.find((p) => p.provider === item.provider)
  return (
    <Card>
      <h2 className="text-sm font-semibold">{title}</h2>
      {note !== undefined && <p className="mt-2 text-sm">{note}</p>}
      {error !== undefined && <p className="mt-2 text-sm text-danger-red">{error}</p>}
      {item.connected ? (
        <div className="mt-2 space-y-1 text-sm">
          {/* `item.connected` alone decides this branch. The spec lookup can
              miss — `providers` comes from the current PROVIDERS registry while
              `provider` comes from the stored row — and when it does, the raw
              key is a worse label but a true one. Saying "Not connected" for a
              live credential would be the same kind of lie the 503 branch
              above refuses to tell. */}
          <p>{connected?.label ?? item.provider}</p>
          {Object.entries(item.identifiers).map(([name, value]) => (
            <p key={name} className="text-ink-muted">
              {name}: <span className="tabular-nums">{value}</span>
            </p>
          ))}
        </div>
      ) : (
        <>
          <p className="mt-2 text-sm text-ink-muted">Not connected</p>
          {item.providers.filter((p) => !p.oauth).map((spec) => (
            <ProviderForm
              key={spec.provider}
              integration={item.integration}
              spec={spec}
              onDone={onDone}
            />
          ))}
          {item.providers.filter((p) => p.oauth).map((spec) => (
            <OauthConnect key={spec.provider} spec={spec} />
          ))}
        </>
      )}
    </Card>
  )
}

export default function IntegrationsPage() {
  const qc = useQueryClient()
  const onDone = () => { void qc.invalidateQueries({ queryKey: ['integrations'] }) }
  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: getIntegrations,
  })

  const search = route.useSearch()
  const navigate = route.useNavigate()
  // Captured on first render: the effect below empties `search`, and reading
  // the note from `search` afterwards would blank it the instant it appeared.
  const landed = useRef({ connected: search.connected, error: search.error })
  useEffect(() => {
    // Shown once. Cleared with `replace` so a reload does not re-announce a
    // grant that completed minutes ago, and so Back does not walk into it.
    if (search.connected !== undefined || search.error !== undefined) {
      void navigate({ search: {}, replace: true })
    }
  }, [search.connected, search.error, navigate])

  // A 503 here is CredentialUnreadable, raised by `get_integrations` in
  // src/usali/integrations_api.py — that is where the whole read is refused
  // rather than an undecryptable row being reported as disconnected. This
  // branch is the frontend half of that refusal: rendering the readable
  // cards beside the message would restore the lie the API declined to
  // tell. Pinned by 'refuses the whole page when a refetch finds a
  // credential cannot be decrypted' in this file's test.
  if (integrations.error instanceof ApiError && integrations.error.status === 503) {
    return (
      <>
        <PageHeader title="Integrations" />
        <Card>
          <p className="text-sm">{integrations.error.detail}</p>
        </Card>
      </>
    )
  }

  return (
    <>
      <PageHeader title="Integrations" />
      {integrations.error !== null && integrations.error !== undefined && (
        <Card><p className="text-sm">{errorMessage(integrations.error)}</p></Card>
      )}
      <div className="space-y-3">
        {(integrations.data?.items ?? []).map((item) => (
          <IntegrationCard
            key={item.integration}
            item={item}
            onDone={onDone}
            note={landed.current.connected === item.integration
              ? `${item.providers[0]?.label ?? item.integration} is connected.`
              : undefined}
            // The callback that sets `error` is accounting's only —
            // `_error_redirect` in src/usali/integrations_api.py is reached
            // solely from the QBO callback — so the accounting card is the
            // one place this param can ever belong.
            error={item.integration === 'accounting' ? landed.current.error : undefined}
          />
        ))}
      </div>
    </>
  )
}
