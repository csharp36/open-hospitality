import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import './index.css'
import { initTheme } from './lib/theme'
import { createAppRouter } from './router'
import { AuthProvider } from './auth/AuthProvider'
import { shouldRetry } from './lib/queryRetry'

// Apply the persisted/OS theme before the first paint so dark mode
// renders without a light flash.
initTheme()

// Retry policy lives in lib/queryRetry (tested there): no retries on a 4xx —
// a 401 would fire another login redirect, and a 403/404/409/422 will not
// change — while a 5xx or network failure gets one retry.
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: shouldRetry } },
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
