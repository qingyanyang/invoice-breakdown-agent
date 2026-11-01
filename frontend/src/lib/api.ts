const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim() ?? ''

/** Public API origin from build-time env (Azure Static Web Apps / local `.env`). */
export const apiBaseUrl = configuredBaseUrl.replace(/\/$/, '')

export function apiUrl(path: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`
  return apiBaseUrl ? `${apiBaseUrl}${normalized}` : normalized
}
