import type { NewsAlert, NewsStock } from './api'

/** The company the news is about: first direct stock, else the first affected stock. */
export function primaryStock(n: NewsAlert): NewsStock | undefined {
  return n.stocks.find((s) => s.relation === 'direct') ?? n.stocks[0]
}

export function columnOf(n: NewsAlert): 'pos' | 'neg' | 'neu' {
  const d = primaryStock(n)?.direction
  return d === 'up' ? 'pos' : d === 'down' ? 'neg' : 'neu'
}

/** A thread: alerts for the same headline company and event type within 60 minutes. */
export type Thread = { key: string; items: NewsAlert[] }   // items newest first
const THREAD_WINDOW_S = 3600

export function threadNews(news: NewsAlert[]): Thread[] {
  const sorted = [...news].sort((a, b) => b.created_at - a.created_at)
  const open = new Map<string, Thread>()
  const out: Thread[] = []
  for (const n of sorted) {
    const k = `${primaryStock(n)?.ticker ?? `id${n.id}`}|${n.event_type}`
    const t = open.get(k)
    // chain within the window: each older alert must be within 60 min of the thread's oldest so far
    if (t && t.items[t.items.length - 1].created_at - n.created_at <= THREAD_WINDOW_S) {
      t.items.push(n)
    } else {
      const nt = { key: `${k}|${n.id}`, items: [n] }
      open.set(k, nt)
      out.push(nt)
    }
  }
  return out
}
