// Global property selection: picked once in the top bar, consumed everywhere.
// The provider lives in Layout; `useGlobalProperty` FALLS BACK to page-local
// state when no provider is mounted, so pages render standalone in tests
// without wrapping. The choice persists per browser (localStorage).

import { createContext, useContext, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'

import { getProperties } from '../api/client'
import type { PropertyInfo } from '../api/types'

const STORAGE_KEY = 'usali.property'

export type GlobalProperty = {
  /** Selected property id; defaults to the first visible property. */
  property: string | undefined
  setProperty: (id: string) => void
  properties: PropertyInfo[] | undefined
  /** The full record for the selected property. */
  selected: PropertyInfo | undefined
}

const Ctx = createContext<GlobalProperty | null>(null)

function usePropertyState(persist: boolean): GlobalProperty {
  const propertiesQuery = useQuery({ queryKey: ['properties'], queryFn: getProperties })
  const [picked, setPicked] = useState<string | undefined>(() =>
    persist ? (localStorage.getItem(STORAGE_KEY) ?? undefined) : undefined,
  )
  const properties = propertiesQuery.data
  // A persisted id that no longer exists (scope change, wiped DB) falls back
  // to the first visible property instead of wedging every page on a 403.
  const valid = picked !== undefined && properties?.some((p) => p.property_id === picked)
  const property = valid ? picked : properties?.[0]?.property_id
  return {
    property,
    setProperty: (id: string) => {
      setPicked(id)
      if (persist) localStorage.setItem(STORAGE_KEY, id)
    },
    properties,
    selected: properties?.find((p) => p.property_id === property),
  }
}

export function GlobalPropertyProvider({ children }: { children: ReactNode }) {
  const value = usePropertyState(true)
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useGlobalProperty(): GlobalProperty {
  const ctx = useContext(Ctx)
  // Fallback keeps hook order stable: the state hook always runs, and is
  // simply ignored when a provider is present.
  const fallback = usePropertyState(false)
  return ctx ?? fallback
}
