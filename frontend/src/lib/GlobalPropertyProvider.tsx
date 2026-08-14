import type { ReactNode } from 'react'

import { GlobalPropertyContext, usePropertyState } from './propertyContext'


export function GlobalPropertyProvider({ children }: { children: ReactNode }) {
  const value = usePropertyState(true)
  return <GlobalPropertyContext.Provider value={value}>{children}</GlobalPropertyContext.Provider>
}
