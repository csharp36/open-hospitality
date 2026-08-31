// Per-tenant integration config (OH-17). One query for the page; the cards
// below are presentational. The provider field lists come from the API — this
// file must never grow one of its own, or it becomes a second copy of
// PROVIDERS with nothing checking it (design doc, section 3).

import { useQuery } from '@tanstack/react-query'

import { ApiError, getIntegrations } from '../api/client'
import type { Integration } from '../api/types'
import { Card, PageHeader } from '../components/ui'
import { errorMessage } from '../lib/errors'

const TITLES: Record<string, string> = {
  payroll: 'Payroll',
  accounting: 'Accounting',
  demand_feed: 'Demand feed',
}

function IntegrationCard({ item }: { item: Integration }) {
  const title = TITLES[item.integration] ?? item.integration
  const connected = item.providers.find((p) => p.provider === item.provider)
  return (
    <Card>
      <h2 className="text-sm font-semibold">{title}</h2>
      {item.connected && connected !== undefined ? (
        <div className="mt-2 space-y-1 text-sm">
          <p>{connected.label}</p>
          {Object.entries(item.identifiers).map(([name, value]) => (
            <p key={name} className="text-ink-muted">
              {name}: <span className="tabular-nums">{value}</span>
            </p>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-sm text-ink-muted">Not connected</p>
      )}
    </Card>
  )
}

export default function IntegrationsPage() {
  const integrations = useQuery({
    queryKey: ['integrations'],
    queryFn: getIntegrations,
  })

  // A 503 here is CredentialUnreadable, raised by `get_integrations` in
  // src/usali/integrations_api.py — that is where the whole read is refused
  // rather than an undecryptable row being reported as disconnected. This
  // branch is the frontend half of that refusal: rendering the readable
  // cards beside the message would restore the lie the API declined to
  // tell. Pinned by 'refuses the whole page when a credential cannot be
  // decrypted' in this file's test.
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
          <IntegrationCard key={item.integration} item={item} />
        ))}
      </div>
    </>
  )
}
