import type { NewsAlert, NewsStock } from './api'

/** The company the news is about: first direct stock, else the first affected stock. */
export function primaryStock(n: NewsAlert): NewsStock | undefined {
  return n.stocks.find((s) => s.relation === 'direct') ?? n.stocks[0]
}

export function columnOf(n: NewsAlert): 'pos' | 'neg' | 'neu' {
  const d = primaryStock(n)?.direction
  return d === 'up' ? 'pos' : d === 'down' ? 'neg' : 'neu'
}
