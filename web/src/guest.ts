// Read-only guest view (/guest?k=<key>). The server sends only whitelisted fields: no headline,
// summary or filing text, and moves as fractions instead of prices (see newsalert/web/guest.py).

export type GuestStock = {
  ticker: string
  name: string
  sector: string
  relation: 'direct' | 'competitor' | 'supplier' | 'customer' | 'sector peer'
  direction: 'up' | 'down' | null
  strength: 'low' | 'medium' | 'high' | null
  move_since_alert: number | null
  ret_15m: number | null
  ret_1h: number | null
  ret_close: number | null
  abn_15m: number | null
  abn_1h: number | null
  abn_close: number | null
}

export type GuestNews = {
  id: number
  created_at: number
  published_at: number | null
  latency_s: number | null
  source: 'nse' | 'businessline'
  source_name: string
  url: string | null
  event_type: string
  classifier: 'rules' | 'gemini'
  confidence: number | null
  reasoning: string
  stocks: GuestStock[]
  linked_price_moves: { symbol: string; direction: 1 | -1; move: number; minutes_after: number }[]
}

export type GuestStatus = {
  server_time: number
  market: { open: boolean; next_open: string | null; timezone: string }
  news: { last_poll_at: number | null; alerts_today: number | null; feeds_ok: number; feeds: number }
}

export type GuestMeta = {
  guest: true
  banner: string
  mode: 'live' | 'demo'
  dataset: string
  index_name: string
  timezone: string
  event_types: string[]
  status: GuestStatus
}

export const guestKey = () => new URLSearchParams(location.search).get('k') ?? ''

function url(path: string, q: Record<string, string | number | undefined> = {}) {
  const p = new URLSearchParams({ k: guestKey() })
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== '') p.set(k, String(v))
  return `/guest/api/${path}?${p}`
}

async function get<T>(path: string, q?: Record<string, string | number | undefined>): Promise<T> {
  const r = await fetch(url(path, q), { credentials: 'omit' })
  if (r.status === 429) throw new Error('Too many requests. Wait a minute and reload.')
  if (!r.ok) throw new Error(r.status === 404 ? 'This demo link is no longer valid.' : `HTTP ${r.status}`)
  return r.json() as Promise<T>
}

export const guestApi = {
  meta: () => get<GuestMeta>('meta'),
  news: (q: Record<string, string | number | undefined>) => get<{ items: GuestNews[]; more: boolean }>('news', q),
  streamUrl: () => url('stream'),
}
