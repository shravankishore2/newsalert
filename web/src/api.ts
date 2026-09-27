// Typed access to the FastAPI backend. All calls send the session cookie (same origin).

export type Reason = { passed: boolean; text: string; [k: string]: unknown }

export type NewsItem = { headline: string; source: string; url: string; published: string | null }

export type Alert = {
  id: number
  mode: 'live' | 'demo'
  symbol: string
  name: string
  sector: string
  ts: number
  direction: 1 | -1
  move: number
  ref_ts: number
  ref_price: number
  price: number
  index_move: number | null
  corr: number | null
  beta: number | null
  residual: number | null
  latency_ms: number | null
  news: NewsItem[]
  reasons: { ma?: Reason; corr?: Reason }
}

export type AlertDetail = Alert & {
  context: { start: number; end: number; prices: [number, number][]; index: [number, number][] }
}

export type Me = {
  mode: 'live' | 'demo'
  dataset: string
  index_name: string
  currency: string
  timezone: string
  params: Record<string, number>
  sectors: string[]
  symbols: string[]
}

type Stamped<T> = { value: T; updated_at: number } | null

export type Status = {
  mode: 'live' | 'demo'
  dataset: string
  server_time: number
  market: { open: boolean; now_ts: number | null; simulated: boolean; next_open?: string | null; timezone: string }
  cycle: Stamped<{ at: number; sim_ts?: number; ok: number; failed: number; alerts: number; error: string | null }>
  token: Stamped<{ state: string; expires_at?: string | null; auto_refresh?: boolean; error?: string | null; note?: string }>
  feeds: Stamped<Record<string, { ok: boolean | null; error: string | null; items?: number; last_ok_at?: number; note?: string }>>
  replay: Stamped<{ dataset: string; sim_ts: number | null; market_open: boolean; speed: number;
    minutes_done: number; total_minutes: number; alerts: number; finished: boolean }>
}

export type ResultRow = { variant: string; alerts: number; evaluable: number; false: number; n: number;
  rate: number; ci_low: number; ci_high: number }
export type ResultSection = { key: string; title: string; markdown: string; false_alert_rows: ResultRow[];
  date_range: string | null; has_numbers: boolean }

export class Unauthorized extends Error {}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin' })
  if (r.status === 401) throw new Unauthorized()
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`)
  return r.json() as Promise<T>
}

export const api = {
  me: () => get<Me>('/api/me'),
  status: () => get<Status>('/api/status'),
  results: () => get<{ sections: ResultSection[] }>('/api/results'),
  alert: (id: number) => get<AlertDetail>(`/api/alerts/${id}`),
  alerts: (q: Record<string, string | number | undefined>) => {
    const p = new URLSearchParams()
    for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== '') p.set(k, String(v))
    return get<{ items: Alert[]; more: boolean }>(`/api/alerts?${p}`)
  },
  async login(password: string): Promise<{ ok: true } | { ok: false; error: string }> {
    const r = await fetch('/api/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }),
    })
    if (r.status === 204) return { ok: true }
    const body = await r.json().catch(() => ({}))
    if (r.status === 429) return { ok: false, error: `Too many attempts. Try again in ${body.retry_after ?? 60} s.` }
    return { ok: false, error: 'Wrong password.' }
  },
  logout: () => fetch('/api/logout', { method: 'POST', credentials: 'same-origin' }),
}

// --- formatting ------------------------------------------------------------------------

export const pct = (x: number | null | undefined, digits = 2) =>
  x === null || x === undefined ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x * 100).toFixed(digits)}%`

export const money = (x: number, currency: string) =>
  `${currency}${x.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

export function fmtTime(ts: number, tz: string, withDate = false) {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, hour: '2-digit', minute: '2-digit',
    ...(withDate ? { day: '2-digit', month: 'short', year: 'numeric' } : {}),
  }).format(new Date(ts * 1000))
}

export function tzAbbr(tz: string) {
  return tz === 'Asia/Kolkata' ? 'IST' : tz === 'America/New_York' ? 'ET' : tz
}

export function ago(seconds: number) {
  if (seconds < 0) seconds = 0
  if (seconds < 60) return `${Math.round(seconds)} s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  return `${(seconds / 3600).toFixed(1)} h ago`
}

export function fmtDate(ts: number, tz: string) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: tz, day: '2-digit', month: 'short', year: 'numeric' })
    .format(new Date(ts * 1000))
}
