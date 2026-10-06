import type { NewsAlert } from './api'

type Threadable = { id: number; created_at: number; event_type: string; stocks: { ticker: string; relation: string }[] }

/** The company the news is about: first direct stock, else the first affected stock. */
export function primaryStock<S extends { relation: string }>(n: { stocks: S[] }): S | undefined {
  return n.stocks.find((s) => s.relation === 'direct') ?? n.stocks[0]
}

export function columnOf(n: NewsAlert): 'pos' | 'neg' | 'neu' {
  const d = primaryStock(n)?.direction
  return d === 'up' ? 'pos' : d === 'down' ? 'neg' : 'neu'
}

/** A thread: alerts for the same headline company and event type within 60 minutes. */
export type Thread<T extends Threadable = NewsAlert> = { key: string; items: T[] }   // items newest first
const THREAD_WINDOW_S = 3600

export function threadNews<T extends Threadable>(news: T[]): Thread<T>[] {
  const sorted = [...news].sort((a, b) => b.created_at - a.created_at)
  const open = new Map<string, Thread<T>>()
  const out: Thread<T>[] = []
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
