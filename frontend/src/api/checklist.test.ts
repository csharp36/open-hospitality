import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getAccessToken, login } from '../auth/oidc'
import { dismissItem, getChecklist, restoreItem } from './checklist'

vi.mock('../auth/oidc', () => ({ getAccessToken: vi.fn(), login: vi.fn() }))

function mockFetch(status: number, body: unknown = null) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(body === null ? null : JSON.stringify(body), {
      status,
      headers: body === null ? undefined : { 'Content-Type': 'application/json' },
    }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.mocked(getAccessToken).mockResolvedValue('tok')
})

describe('checklist API', () => {
  it('GETs /api/checklist and returns the parsed body', async () => {
    const f = mockFetch(200, { items: [], open_count: 0, error_count: 0, all_clear: true })
    const out = await getChecklist()
    expect(out.all_clear).toBe(true)
    expect(f.mock.calls[0]![0]).toBe('/api/checklist')
    expect(new Headers(f.mock.calls[0]![1]!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('dismissItem PUTs the dismissal and tolerates a 204 with no body', async () => {
    const f = mockFetch(204)
    await expect(dismissItem('payroll')).resolves.toBeUndefined()
    const [url, init] = f.mock.calls[0]!
    expect(url).toBe('/api/checklist/payroll/dismissal')
    expect((init as RequestInit).method).toBe('PUT')
    expect(new Headers((init as RequestInit).headers).get('Authorization')).toBe('Bearer tok')
  })

  it('restoreItem DELETEs the dismissal', async () => {
    const f = mockFetch(204)
    await restoreItem('payroll')
    const init = f.mock.calls[0]![1] as RequestInit
    expect(init.method).toBe('DELETE')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('surfaces the 422 detail when a required item is dismissed', async () => {
    mockFetch(422, { detail: 'first_report is required and cannot be dismissed' })
    await expect(dismissItem('first_report')).rejects.toMatchObject({
      status: 422,
      detail: expect.stringContaining('cannot be dismissed'),
    })
  })

  // ONE 401 test only: `redirecting` in client.ts is a module-level latch, so a
  // second 401 in this file would see login already fired and assert nothing.
  it('a 401 on a write redirects to login and still rejects', async () => {
    mockFetch(401)
    await expect(dismissItem('payroll')).rejects.toThrow()
    expect(login).toHaveBeenCalled()
  })
})
