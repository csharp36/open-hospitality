import { describe, expect, it, vi, beforeEach } from 'vitest'
import * as oidc from '../auth/oidc'
import { getEmployees, getMe, onboardEmployee, terminateEmployee } from './client'

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(oidc, 'getAccessToken').mockResolvedValue('tok')
})

describe('employees api', () => {
  it('getEmployees GETs /api/employees with bearer', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    )
    await getEmployees()
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/employees')
    expect(new Headers(init!.headers).get('Authorization')).toBe('Bearer tok')
  })

  it('onboardEmployee POSTs the body as JSON', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ employee_id: 1 }), { status: 201 }),
    )
    await onboardEmployee({ full_name: 'A', email: 'a@x.com', property: 'HISJ',
      pay_type: 'salary', role: 'property_gm', department_id: null })
    const init = fetchMock.mock.calls[0]![1]!
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string).property).toBe('HISJ')
  })

  it('terminateEmployee POSTs to the terminate path', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ employee_id: 5 }), { status: 200 }),
    )
    await terminateEmployee(5)
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/employees/5/terminate')
  })

  it('getMe GETs /api/me', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ subject: 's', username: 'u', roles: [] }), { status: 200 }),
    )
    await getMe()
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/me')
  })
})
