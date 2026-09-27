import { useEffect, useMemo, useState } from 'react'
import {
  api, fmtDate, fmtDuration, fmtTime, pct, SOURCE_LABEL, tzAbbr,
  type Alert, type Me, type NewsAlert, type NewsStock,
} from '../api'
import type { Conn } from '../stream'
import Feed, { Direction } from './Feed'

type Layer = 'news' | 'both' | 'price'
type Filters = { symbol: string; sector: string; direction: '' | 'up' | 'down'; event_type: string; q: string }
const EMPTY: Filters = { symbol: '', sector: '', direction: '', event_type: '', q: '' }

const LAYER_KEY = 'newsalert.layer'
function initialLayer(): Layer {
  try {
    const v = localStorage.getItem(LAYER_KEY)
    return v === 'both' || v === 'price' ? v : 'news'
  } catch { return 'news' }
}

function newsMatches(n: NewsAlert, f: Filters) {
  if (f.event_type && n.event_type !== f.event_type) return false
  const stocks = n.stocks.filter((s) =>
    (!f.symbol || s.ticker === f.symbol.toUpperCase()) && (!f.sector || s.sector === f.sector) &&
    (!f.direction || s.direction === f.direction))
  if ((f.symbol || f.sector || f.direction) && stocks.length === 0) return false
  if (f.q) {
    const q = f.q.toLowerCase()
    const hay = [n.headline ?? '', ...n.stocks.flatMap((s) => [s.ticker, s.name])].join(' ').toLowerCase()
    if (!hay.includes(q)) return false
  }
  return true
}

function priceMatches(a: Alert, f: Filters) {
  if (f.symbol && a.symbol !== f.symbol.toUpperCase()) return false
  if (f.sector && a.sector !== f.sector) return false
  if (f.direction && (a.direction > 0 ? 'up' : 'down') !== f.direction) return false
  if (f.q && ![a.symbol, a.name].join(' ').toLowerCase().includes(f.q.toLowerCase())) return false
  return true
}

export function StockChip({ s }: { s: NewsStock }) {
  const dir = s.direction === 'up' ? 'up' : s.direction === 'down' ? 'down' : 'none'
  return (
    <span className={`chip ${dir}`} title={s.reason ?? undefined}>
      <span aria-hidden="true">{s.direction === 'up' ? '▲' : s.direction === 'down' ? '▼' : '•'}</span>
      <strong>{s.ticker}</strong>
      <span className="chip-meta">{s.direction ?? 'no direction'} · {s.relation}{s.strength ? ` · ${s.strength}` : ''}</span>
    </span>
  )
}

export function EventBadge({ type }: { type: string }) {
  return <span className="event-badge">{type}</span>
}

function NewsCard({ n, me, fresh }: { n: NewsAlert; me: Me; fresh: boolean }) {
  const tz = me.timezone
  const direct = n.stocks.filter((s) => s.relation === 'direct')
  const second = n.stocks.filter((s) => s.relation !== 'direct')
  return (
    <a className={`news-card${fresh ? ' fresh' : ''}`} href={`#/news/${n.id}`}>
      <div className="news-top">
        <EventBadge type={n.event_type} />
        <span className="muted">{SOURCE_LABEL[n.source] ?? n.source}</span>
        {n.classifier === 'rules' && <span className="muted" title="NSE filings are classified by local rules, never sent to an LLM">· rules</span>}
        <span className="spacer" />
        <span className="when num">
          {fmtTime(n.created_at, tz)} {tzAbbr(tz)} <span className="muted">{fmtDate(n.created_at, tz)}</span>
        </span>
      </div>
      <div className="headline">{n.headline ?? '(no headline)'}</div>
      <div className="chips">
        {direct.map((s) => <StockChip key={`${s.ticker}-d`} s={s} />)}
        {second.map((s) => <StockChip key={`${s.ticker}-${s.relation}`} s={s} />)}
      </div>
      {n.stocks[0]?.reason && n.classifier === 'gemini' && <div className="reason-line">{n.stocks[0].reason}</div>}
      <div className="news-foot muted">
        {n.latency_s !== null && <span>alerted {fmtDuration(n.latency_s)} after publication</span>}
        {n.confidence !== null && <span>confidence {Math.round(n.confidence * 100)}%</span>}
        {n.linked_price_alerts.length > 0 && (
          <span className="linked">↳ {n.linked_price_alerts.length} linked price move{n.linked_price_alerts.length > 1 ? 's' : ''}:{' '}
            {n.linked_price_alerts.map((p) => `${p.symbol} ${pct(p.move)}`).join(', ')}</span>
        )}
      </div>
    </a>
  )
}

function PriceCardSmall({ a, me }: { a: Alert; me: Me }) {
  return (
    <a className="price-card" href={`#/alerts/${a.id}`}>
      <span className="price-tag">Price move</span>
      <strong>{a.symbol}</strong>
      <Direction d={a.direction} />
      <span className="num">{pct(a.move)}</span>
      {a.linked_news.length > 0 && <span className="muted">↳ {a.linked_news[0].minutes_after.toFixed(0)} min after news</span>}
      <span className="spacer" />
      <span className="muted num">{fmtTime(a.ts, me.timezone)} {tzAbbr(me.timezone)}</span>
    </a>
  )
}

export default function NewsFeed({ me, pushedNews, pushedPrice, conn }:
  { me: Me; pushedNews: NewsAlert[]; pushedPrice: Alert[]; conn: Conn }) {
  const [layer, setLayerState] = useState<Layer>(initialLayer)
  const setLayer = (l: Layer) => { setLayerState(l); try { localStorage.setItem(LAYER_KEY, l) } catch { /* ignore */ } }
  const [filters, setFilters] = useState<Filters>(EMPTY)
  const [qInput, setQInput] = useState('')
  const [news, setNews] = useState<NewsAlert[]>([])
  const [price, setPrice] = useState<Alert[]>([])
  const [more, setMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [base, setBase] = useState({ news: 0, price: 0 })

  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: qInput.trim() })), 300)
    return () => clearTimeout(t)
  }, [qInput])

  useEffect(() => {
    if (layer === 'price') return
    let cancelled = false
    setLoading(true)
    setError('')
    const q = { symbol: filters.symbol, sector: filters.sector, direction: filters.direction, q: filters.q, limit: 50 }
    Promise.all([
      api.news({ ...q, event_type: filters.event_type }),
      layer === 'both' && !filters.event_type ? api.alerts(q) : Promise.resolve({ items: [] as Alert[], more: false }),
    ]).then(([n, p]) => {
      if (cancelled) return
      setNews(n.items)
      setPrice(p.items)
      setMore(n.more)
      setBase({
        news: Math.max(0, ...n.items.map((x) => x.id), ...pushedNews.map((x) => x.id)),
        price: Math.max(0, ...p.items.map((x) => x.id), ...pushedPrice.map((x) => x.id)),
      })
    }).catch((e) => !cancelled && setError(String(e))).finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, layer])

  const liveNews = useMemo(() => pushedNews.filter((n) => n.id > base.news && newsMatches(n, filters)), [pushedNews, base, filters])
  const livePrice = useMemo(() => layer === 'both' && !filters.event_type
    ? pushedPrice.filter((a) => a.id > base.price && priceMatches(a, filters)) : [], [pushedPrice, base, filters, layer])
  const freshIds = useMemo(() => new Set(liveNews.map((n) => n.id)), [liveNews])

  type Row = { kind: 'news'; t: number; n: NewsAlert } | { kind: 'price'; t: number; a: Alert }
  const rows: Row[] = useMemo(() => {
    const seenN = new Set<number>(), seenP = new Set<number>()
    const out: Row[] = []
    for (const n of [...liveNews, ...news]) if (!seenN.has(n.id)) { seenN.add(n.id); out.push({ kind: 'news', t: n.created_at, n }) }
    for (const a of [...livePrice, ...price]) if (!seenP.has(a.id)) { seenP.add(a.id); out.push({ kind: 'price', t: a.ts, a }) }
    return out.sort((x, y) => y.t - x.t)
  }, [liveNews, news, livePrice, price])

  async function loadMore() {
    const last = news[news.length - 1]
    if (!last) return
    const r = await api.news({ ...filters, before_id: last.id, limit: 50 })
    setNews((cur) => [...cur, ...r.items])
    setMore(r.more)
  }

  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }))
  const active = filters.symbol || filters.sector || filters.direction || filters.event_type || filters.q

  return (
    <>
      <div className="feed-head">
        <h1>News alerts</h1>
        <span className="live-pill"><span className={`dot ${conn === 'open' ? 'good' : 'warn'}`} aria-hidden="true" />
          {conn === 'open' ? 'Live: new alerts appear automatically' : 'Reconnecting…'}</span>
        <span className="spacer" />
        <div className="seg" role="group" aria-label="Feed layers">
          {([['news', 'News'], ['both', 'News + price moves'], ['price', 'Price moves']] as const).map(([k, label]) => (
            <button key={k} type="button" aria-pressed={layer === k} onClick={() => setLayer(k)}>{label}</button>
          ))}
        </div>
      </div>

      {layer === 'price' ? <Feed me={me} pushed={pushedPrice} conn={conn} /> : (
        <>
          <form className="filters" role="search" onSubmit={(e) => e.preventDefault()}>
            <div className="field">
              <label htmlFor="n-sym">Ticker</label>
              <input id="n-sym" list="n-symbols" placeholder="Any" size={10} value={filters.symbol}
                onChange={(e) => set({ symbol: e.target.value.toUpperCase() })} />
              <datalist id="n-symbols">{me.symbols.map((s) => <option key={s} value={s} />)}</datalist>
            </div>
            <div className="field">
              <label htmlFor="n-sector">Sector</label>
              <select id="n-sector" value={filters.sector} onChange={(e) => set({ sector: e.target.value })}>
                <option value="">All sectors</option>
                {me.sectors.map((s) => <option key={s}>{s}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="n-event">Event</label>
              <select id="n-event" value={filters.event_type} onChange={(e) => set({ event_type: e.target.value })}>
                <option value="">All events</option>
                {me.event_types.map((s) => <option key={s}>{s}</option>)}
              </select>
            </div>
            <div className="field">
              <span id="n-dir" style={{ fontSize: 12, color: 'var(--text-muted)' }}>Direction</span>
              <div className="seg" role="group" aria-labelledby="n-dir">
                {(['', 'up', 'down'] as const).map((d) => (
                  <button key={d} type="button" aria-pressed={filters.direction === d} onClick={() => set({ direction: d })}>
                    {d === '' ? 'Both' : d === 'up' ? '▲ Up' : '▼ Down'}
                  </button>
                ))}
              </div>
            </div>
            <div className="field grow">
              <label htmlFor="n-q">Search past alerts</label>
              <input id="n-q" type="search" placeholder="Headline, ticker or company" value={qInput}
                onChange={(e) => setQInput(e.target.value)} />
            </div>
            {active && <button type="button" className="btn" onClick={() => { setFilters(EMPTY); setQInput('') }}>Clear</button>}
          </form>
          {layer === 'both' && filters.event_type && (
            <p className="muted" style={{ marginTop: -4 }}>Price moves are hidden while an event filter is set.</p>
          )}
          {error && <div className="empty error">{error}</div>}
          {!error && !loading && rows.length === 0 && (
            <div className="empty">{active ? 'No alerts match these filters.'
              : 'No news alerts yet. They appear here as soon as a filing or headline is classified.'}</div>
          )}
          <ol className="feed" aria-live="polite" aria-busy={loading}>
            {rows.map((r) => (
              <li key={r.kind === 'news' ? `n${r.n.id}` : `p${r.a.id}`}>
                {r.kind === 'news' ? <NewsCard n={r.n} me={me} fresh={freshIds.has(r.n.id)} /> : <PriceCardSmall a={r.a} me={me} />}
              </li>
            ))}
          </ol>
          {more && <div className="more"><button className="btn" onClick={loadMore}>Load older news alerts</button></div>}
        </>
      )}
    </>
  )
}
