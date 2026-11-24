import { useEffect, useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { apiBaseUrl, apiUrl } from '@/lib/api'

type HealthState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ok'; detail: string }
  | { status: 'error'; detail: string }

function HomePage() {
  const [health, setHealth] = useState<HealthState>({ status: 'idle' })

  useEffect(() => {
    if (!apiBaseUrl) {
      return
    }

    const controller = new AbortController()

    async function checkHealth() {
      setHealth({ status: 'loading' })
      try {
        const response = await fetch(apiUrl('/health'), {
          signal: controller.signal,
        })
        if (!response.ok) {
          setHealth({
            status: 'error',
            detail: `HTTP ${response.status}`,
          })
          return
        }
        const body = (await response.json().catch(() => null)) as
          | { status?: string }
          | null
        setHealth({
          status: 'ok',
          detail: body?.status ?? 'reachable',
        })
      } catch (error) {
        if (controller.signal.aborted) {
          return
        }
        setHealth({
          status: 'error',
          detail: error instanceof Error ? error.message : 'Request failed',
        })
      }
    }

    void checkHealth()
    return () => controller.abort()
  }, [])

  return (
    <div className="relative min-h-svh overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_top,_oklch(0.92_0.04_160)_0%,_transparent_55%),linear-gradient(180deg,_oklch(0.985_0.01_160)_0%,_oklch(0.96_0.03_85)_100%)]"
      />
      <main className="relative mx-auto flex min-h-svh w-full max-w-3xl flex-col justify-center px-6 py-16">
        <h1 className="text-4xl font-semibold tracking-tight text-foreground sm:text-5xl">
          AFLUX Invoice Breakdown Agent
        </h1>
        <p className="mt-4 max-w-2xl text-lg text-muted-foreground">
          Check the connection to the Invoice Breakdown API.
        </p>

        <dl className="mt-12 grid gap-4 rounded-xl border border-border bg-card/80 p-5 text-sm shadow-sm backdrop-blur">
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <dt className="text-muted-foreground">API base URL</dt>
            <dd className="font-mono text-foreground">
              {apiBaseUrl || 'not configured (set VITE_API_BASE_URL)'}
            </dd>
          </div>
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <dt className="text-muted-foreground">/health</dt>
            <dd className="font-mono text-foreground">
              {health.status === 'idle' && 'skipped (no API URL)'}
              {health.status === 'loading' && 'checking…'}
              {health.status === 'ok' && `ok · ${health.detail}`}
              {health.status === 'error' && `error · ${health.detail}`}
            </dd>
          </div>
        </dl>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
      </Routes>
    </BrowserRouter>
  )
}
