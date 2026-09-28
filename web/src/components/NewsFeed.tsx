import { useEffect, useMemo, useState } from 'react'
import {
  api, fmtDate, fmtDuration, fmtTime, pct, SOURCE_LABEL, tzAbbr,
  type Alert, type Me, type NewsAlert, type NewsStock,
} from '../api'
import type { Conn } from '../stream'
import Feed, { Direction } from './Feed'
import { columnOf, primaryStock } from '../news'

type Layer = 'news' | 'both' | 'price'
type Filters = { symbol: string; sector: string; direction: '' | 'up' | 'down'; event_type: string; q: string }
const EMPTY: Filters = { symbol: '', sector: '', direction: '', event_type: '', q: '' }

const LAYER_KEY = 'quantradar.layer'
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
      <span className="chip-meta">{s.relation}</span>
    </span>
  )
}

export function EventBadge({ type }: { type: string }) {
  return <span className="event-badge">{type}</span>
}

/** One compact badge instead of separate strength / relation / confidence tags. */
export function StrengthBadge({ n }: { n: NewsAlert }) {
  const p = primaryStock(n)
  const strength = p?.strength ? p.strength[0].toUpperCase() + p.strength.slice(1) : '—'
  const conf = n.classifier === 'rules' ? 'rules' : n.confidence !== null ? `${Math.round(n.confidence * 100)}%` : ''
  return <span className={`strength s-${p?.strength ?? 'none'}`}
    title={`Strength ${strength.toLowerCase()} · ${n.classifier === 'rules' ? 'classified by NSE filing rules' : `classification confidence ${conf}`}`}>
    {strength}{conf ? ` · ${conf}` : ''}</span>
}

// Rule labels that only restate the event type; the badge already says it.
const GENERIC_NSE_LABELS = new Set(['financial results', 'regulatory action/order', 'management change', 'order/contract win',
  'credit rating update', 'merger/acquisition filing', 'capital raise/allotment', 'fraud/default/legal filing',
  'dividend/buyback', 'guidance/outlook', 'other filing'])

function eventLine(n: NewsAlert, p: NewsStock | undefined): string | null {
  if (n.source === 'nse' && n.headline) {
    // NSE alerts carry "TICKER: Label"; the company is in the header, and generic labels repeat the badge
    const label = p && n.headline.startsWith(`${p.ticker}: `) ? n.headline.slice(p.ticker.length + 2) : n.headline
    return GENERIC_NSE_LABELS.has(label.toLowerCase()) ? null : label
  }
  return n.headline ?? '(no headline)'
}

function NewsCard({ n, me, fresh }: { n: NewsAlert; me: Me; fresh: boolean }) {
  const tz = me.timezone
  const p = primaryStock(n)
  const others = n.stocks.filter((s) => s !== p && s.ticker !== p?.ticker)
  const col = columnOf(n)
  return (
    <a className={`news-card tone-${col}${fresh ? ' fresh' : ''}`} href={`#/news/${n.id}`}>
      <div className="nc-head">
        <span className="nc-company">{p?.name || p?.ticker || 'Market'}{p && <span className="nc-ticker"> · {p.ticker}</span>}</span>
        <StrengthBadge n={n} />
      </div>
      <div className="nc-event">
        <EventBadge type={n.event_type} />
        {eventLine(n, p) && <span className="nc-line">{eventLine(n, p)}</span>}
      </div>
      {n.classifier === 'gemini' && p?.reason && <div className="reason-line">{p.reason}</div>}
      {others.length > 0 && (
        <div className="chips" aria-label="Other affected stocks">
          {others.map((s) => <StockChip key={`${s.ticker}-${s.relation}`} s={s} />)}
        </div>
      )}
      <div className="news-foot">
        <span className="num">{fmtTime(n.created_at, tz)} {tzAbbr(tz)} · {fmtDate(n.created_at, tz)}</span>
        <span>{SOURCE_LABEL[n.source] ?? n.source}</span>
        {n.latency_s !== null && <span>alerted {fmtDuration(n.latency_s)} after publication</span>}
        {n.linked_price_alerts.length > 0 && (
          <span className="linked">↳ {n.linked_price_alerts.map((x) => `${x.symbol} ${pct(x.move)}`).join(', ')}</span>
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

type Row = { kind: 'news'; t: number; n: NewsAlert } | { kind: 'price'; t: number; a: Alert }
const NEU_KEY = 'quantradar.neutralCollapsed'

function Columns({ rows, me, freshIds, loading }: { rows: Row[]; me: Me; freshIds: Set<number>; loading: boolean }) {
  const [tab, setTab] = useState<'pos' | 'neg' | 'neu'>('pos')
  const [neuCollapsed, setNeuCollapsed] = useState(() => {
    try { return localStorage.getItem(NEU_KEY) === '1' } catch { return false }
  })
  const toggleNeu = () => setNeuCollapsed((v) => {
    try { localStorage.setItem(NEU_KEY, v ? '0' : '1') } catch { /* ignore */ }
    return !v
  })
  const col = (r: Row) => r.kind === 'news' ? columnOf(r.n) : (r.a.direction > 0 ? 'pos' : 'neg')
  const groups = { pos: rows.filter((r) => col(r) === 'pos'), neg: rows.filter((r) => col(r) === 'neg'),
    neu: rows.filter((r) => col(r) === 'neu') }
  const count = (k: 'pos' | 'neg' | 'neu') => groups[k].filter((r) => r.kind === 'news').length
  const list = (k: 'pos' | 'neg' | 'neu') => (
    <ol className="feed" aria-busy={loading}>
      {groups[k].map((r) => (
        <li key={r.kind === 'news' ? `n${r.n.id}` : `p${r.a.id}`}>
          {r.kind === 'news' ? <NewsCard n={r.n} me={me} fresh={freshIds.has(r.n.id)} /> : <PriceCardSmall a={r.a} me={me} />}
        </li>
      ))}
      {groups[k].length === 0 && <li className="col-empty">Nothing here yet.</li>}
    </ol>
  )
  const TITLES = { pos: 'Positive', neg: 'Negative', neu: 'Neutral / watch' }
  return (
    <>
      <div className="col-tabs" role="tablist" aria-label="News columns">
        {(['pos', 'neg', 'neu'] as const).map((k) => (
          <button key={k} role="tab" aria-selected={tab === k} className={`col-tab t-${k}`} onClick={() => setTab(k)}>
            {TITLES[k]} <span className="count">{count(k)}</span>
          </button>
        ))}
      </div>
      <div className={`columns${neuCollapsed ? ' neu-collapsed' : ''}`} data-tab={tab} aria-live="polite">
        <section className="col col-pos" aria-labelledby="h-pos">
          <h2 id="h-pos" className="col-head"><span className="col-dot" aria-hidden="true">▲</span>Positive <span className="count">{count('pos')}</span></h2>
          {list('pos')}
        </section>
        <section className="col col-neg" aria-labelledby="h-neg">
          <h2 id="h-neg" className="col-head"><span className="col-dot" aria-hidden="true">▼</span>Negative <span className="count">{count('neg')}</span></h2>
          {list('neg')}
        </section>
        <section className="col col-neu" aria-labelledby="h-neu">
          <h2 id="h-neu" className="col-head">
            <button type="button" className="col-toggle" aria-expanded={!neuCollapsed} onClick={toggleNeu}
              title={neuCollapsed ? 'Expand' : 'Collapse'}>{neuCollapsed ? '◂' : '▸'}</button>
            <span className="col-neu-title">Neutral / watch <span className="count">{count('neu')}</span></span>
          </h2>
          {!neuCollapsed && list('neu')}
        </section>
      </div>
    </>
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
          <Columns rows={rows} me={me} freshIds={freshIds} loading={loading} />
          {more && <div className="more"><button className="btn" onClick={loadMore}>Load older news alerts</button></div>}
        </>
      )}
    </>
  )
}
