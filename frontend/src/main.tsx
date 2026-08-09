import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import './index.css'
import { initTheme } from './lib/theme'
import { createAppRouter } from './router'
import { AuthProvider } from './auth/AuthProvider'
import { ApiError } from './api/client'

// Apply the persisted/OS theme before the first paint so dark mode
// renders without a light flash.
initTheme()

// Do not retry on 401 — a 401 means the session expired (A1 has no refresh),
// and each retry would fire another login redirect. Other errors keep the
// default 3-retry behavior.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, err) =>
        err instanceof ApiError && err.status === 401 ? false : count < 3,
    },
  },
})
const router = createAppRouter()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
)
