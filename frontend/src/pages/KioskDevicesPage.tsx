import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Badge, Card, PageHeader, cellClass, controlClass, headCellClass, tableClass,
} from '../components/ui'
import {
  enrollKioskDevice, getKioskDevices, getProperties, revokeKioskDevice,
} from '../api/client'
import type { EnrolledKiosk } from '../api/types'
import { errorMessage } from '../lib/errors'

/**
 * Enroll / revoke the tablets that run the time clock. The device token is
 * returned by the API exactly ONCE, at enrollment — it is shown here until
 * dismissed and can never be retrieved again (re-enroll instead). A revoked
 * device stops authenticating immediately and falls back to its enrollment
 * screen on the next request.
 */
export default function KioskDevicesPage() {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [property, setProperty] = useState('')
  const [minted, setMinted] = useState<EnrolledKiosk | null>(null)

  const devices = useQuery({ queryKey: ['kiosk-devices'], queryFn: getKioskDevices })
  const properties = useQuery({ queryKey: ['properties'], queryFn: getProperties })

  const enroll = useMutation({
    mutationFn: () => enrollKioskDevice(property, name),
    onSuccess: (result) => {
      setMinted(result)
      setName('')
      void qc.invalidateQueries({ queryKey: ['kiosk-devices'] })
    },
  })
  const revoke = useMutation({
    mutationFn: (id: number) => revokeKioskDevice(id),
    onSettled: () => void qc.invalidateQueries({ queryKey: ['kiosk-devices'] }),
  })

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Kiosk devices"
        subtitle="The tablets running the time clock. Enroll one per hotel — a shared tablet gets one enrollment per hotel it serves."
      />

      <Card role="region" aria-label="enroll kiosk">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            enroll.mutate()
          }}
        >
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">Property</span>
            <select
              className={controlClass}
              value={property}
              aria-label="Property"
              onChange={(e) => setProperty(e.target.value)}
              required
            >
              <option value="" disabled>Select…</option>
              {(properties.data ?? []).map((p) => (
                <option key={p.property_id} value={p.property_id}>{p.property_id}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-xs font-medium text-ink-muted">Device name</span>
            <input
              className={controlClass}
              value={name}
              aria-label="Device name"
              onChange={(e) => setName(e.target.value)}
              placeholder="Front desk iPad"
              required
            />
          </label>
          <button
            type="submit"
            disabled={enroll.isPending}
            className="rounded-control bg-accent px-3 py-2 text-sm font-medium text-accent-contrast disabled:opacity-50"
          >
            Enroll device
          </button>
        </form>
        {enroll.isError && (
          <p className="mt-2 text-sm text-danger-red">
            Enroll failed: {errorMessage(enroll.error)}
          </p>
        )}
        {minted && (
          <div className="mt-3 rounded-card border border-line bg-surface-sunken p-3">
            <p className="text-sm font-medium text-ink">
              Token for “{minted.name}” ({minted.property_id}) — shown once, never again:
            </p>
            <code className="my-2 block break-all rounded-control bg-surface-raised p-2 text-sm">
              {minted.token}
            </code>
            <p className="text-xs text-ink-muted">
              On the tablet, open /kiosk and paste this token to enroll. If it is
              lost, revoke the device and enroll again.
            </p>
            <button
              type="button"
              onClick={() => setMinted(null)}
              className="mt-2 rounded-control border border-line px-2 py-1 text-xs text-ink-muted"
            >
              Dismiss
            </button>
          </div>
        )}
      </Card>

      <Card role="region" aria-label="kiosk devices">
        {devices.isPending && <p className="text-sm text-ink-muted">Loading …</p>}
        {devices.isError && (
          <p className="text-sm text-danger-red">
            Failed to load: {errorMessage(devices.error)}
          </p>
        )}
        {revoke.isError && (
          <p className="mb-2 text-sm text-danger-red">
            Revoke failed: {errorMessage(revoke.error)}
          </p>
        )}
        {devices.data && (
          <table className={tableClass} aria-label="Kiosk devices">
            <thead>
              <tr className="border-b border-line">
                <th className={headCellClass}>Property</th>
                <th className={headCellClass}>Name</th>
                <th className={headCellClass}>Status</th>
                <th className={headCellClass}></th>
              </tr>
            </thead>
            <tbody>
              {devices.data.map((d) => (
                <tr key={d.device_id} className="border-b border-line last:border-0">
                  <td className={cellClass}>{d.property_id}</td>
                  <td className={cellClass}>{d.name}</td>
                  <td className={cellClass}>
                    {d.revoked
                      ? <Badge tone="neutral">revoked</Badge>
                      : <Badge tone="ok">active</Badge>}
                  </td>
                  <td className={cellClass}>
                    {!d.revoked && (
                      <button
                        type="button"
                        disabled={revoke.isPending}
                        onClick={() => revoke.mutate(d.device_id)}
                        className="rounded-control border border-line px-2 py-1 text-xs text-danger-red hover:bg-surface-sunken disabled:opacity-50"
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
