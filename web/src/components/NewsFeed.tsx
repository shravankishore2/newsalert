import { useEffect, useMemo, useState, type MouseEvent } from 'react'
import {
  api, fmtDate, fmtDuration, fmtTime, pct, tzAbbr,
  type Alert, type Me, type NewsAlert, type NewsStock,
} from '../api'
import type { Conn } from '../stream'
import Feed, { Direction } from './Feed'
import Link from './Link'
import { columnOf, primaryStock, threadNews, type Thread } from '../news'

type Layer = 'news' | 'both' | 'price'
type Filters = { sector: string; event_type: string; q: string }
const EMPTY: Filters = { sector: '', event_type: '', q: '' }
const VIEW_TITLE: Record<Layer, string> = { news: 'News alerts', both: 'News & price moves', price: 'Price-move alerts' }
const SOURCE_NAME: Record<string, string> = { nse: 'NSE', businessline: 'BusinessLine' }

const LAYER_KEY = 'quantradar.layer'
function initialLayer(): Layer {
  try {
    const v = localStorage.getItem(LAYER_KEY)
    return v === 'both' || v === 'price' ? v : 'news'
  } catch { return 'news' }
}

function newsMatches(n: NewsAlert, f: Filters) {
  if (f.event_type && n.event_type !== f.event_type) return false
  if (f.sector && !n.stocks.some((s) => s.sector === f.sector)) return false
  if (f.q) {
    const q = f.q.toLowerCase()
    const hay = [n.headline ?? '', n.summary ?? '', ...n.stocks.flatMap((s) => [s.ticker, s.name])].join(' ').toLowerCase()
    if (!hay.includes(q)) return false
  }
  return true
}

function priceMatches(a: Alert, f: Filters) {
  if (f.sector && a.sector !== f.sector) return false
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

/** The card's focal point: direction in strong colour, plus the expected impact strength. */
export function ImpactBadge({ s }: { s: NewsStock | undefined }) {
  const d = s?.direction
  const strength = s?.strength ? s.strength[0].toUpperCase() + s.strength.slice(1) : null
  const label = d === 'up' ? 'Up' : d === 'down' ? 'Down' : 'Neutral'
  return (
    <span className={`impact impact-${d ?? 'none'}`} title="Expected direction and impact strength for the headline company">
      <span aria-hidden="true">{d === 'up' ? '▲' : d === 'down' ? '▼' : '•'}</span>
      {label}{strength && <> · {strength}<span className="impact-word"> impact</span></>}
    </span>
  )
}

// Rule labels that only restate the event type (the badge already says it).
const GENERIC_NSE_LABELS = new Set(['financial results', 'regulatory action/order', 'management change', 'order/contract win',
  'credit rating update', 'merger/acquisition filing', 'capital raise/allotment', 'fraud/default/legal filing',
  'dividend/buyback', 'guidance/outlook', 'other filing'])

function headlineOf(n: NewsAlert, p: NewsStock | undefined): string {
  if (n.source === 'nse') {
    const raw = n.headline ?? ''
    const label = p && raw.startsWith(`${p.ticker}: `) ? raw.slice(p.ticker.length + 2) : raw
    return GENERIC_NSE_LABELS.has(label.toLowerCase()) || !label ? 'Exchange filing' : `Exchange filing: ${label}`
  }
  return n.headline ?? '(no headline)'
}

function NewsCard({ thread, me, fresh }: { thread: Thread; me: Me; fresh: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const [open, setOpen] = useState(false)
  const n = thread.items[0]
  const tz = me.timezone
  const p = primaryStock(n)
  const others = n.stocks.filter((s) => s !== p && s.ticker !== p?.ticker)
  const col = columnOf(n)
  const summary = n.summary ?? (n.classifier === 'gemini' ? p?.reason ?? null : null)
  const long = (summary?.length ?? 0) > 150
  const stop = (fn: () => void) => (e: MouseEvent) => { e.preventDefault(); e.stopPropagation(); fn() }
  return (
    <article className={`news-card tone-${col}${fresh ? ' fresh' : ''}`}>
      <div className="nc-top">
        <EventBadge type={n.event_type} />
        <ImpactBadge s={p} />
      </div>
      <div className="nc-company">{p?.name || p?.ticker || 'Market'}{p && <span className="nc-ticker"> · {p.ticker}</span>}</div>
      <h3 className="nc-headline"><Link to={`/news/${n.id}`} className="stretched">{headlineOf(n, p)}</Link></h3>
      {summary && (
        <div className="nc-summary-wrap">
          <p className={`nc-summary${expanded ? '' : ' clamp'}`}>{summary}</p>
          {long && <button type="button" className="linkish above" aria-expanded={expanded}
            onClick={stop(() => setExpanded((v) => !v))}>{expanded ? 'Show less' : 'Show more'}</button>}
        </div>
      )}
      {others.length > 0 && (
        <div className="chips" aria-label="Other affected stocks">
          {others.map((s) => <StockChip key={`${s.ticker}-${s.relation}`} s={s} />)}
        </div>
      )}
      <footer className="nc-foot">
        <span className="num">{fmtTime(n.created_at, tz)} {tzAbbr(tz)}, {fmtDate(n.created_at, tz)}</span>
        <span>{SOURCE_NAME[n.source] ?? n.source}</span>
        {n.latency_s !== null && <span>alerted {fmtDuration(n.latency_s)} after publication</span>}
        <span>{n.classifier === 'rules' ? 'rule-based' : n.confidence !== null ? `${Math.round(n.confidence * 100)}% confidence` : ''}</span>
        {n.linked_price_alerts.length > 0 && (
          <span>↳ {n.linked_price_alerts.map((x) => `${x.symbol} ${pct(x.move)}`).join(', ')}</span>
        )}
      </footer>
      {thread.items.length > 1 && (
        <div className="nc-thread above">
          <button type="button" className="thread-toggle" aria-expanded={open} onClick={stop(() => setOpen((v) => !v))}>
            {thread.items.length} updates · {open ? 'hide' : 'show all'} {open ? '▴' : '▾'}
          </button>
          {open && (
            <ol className="thread-list">
              {thread.items.map((t) => (
                <li key={t.id}>
                  <Link to={`/news/${t.id}`}>
                    <span className="num">{fmtTime(t.created_at, tz)}</span> {headlineOf(t, primaryStock(t))}
                  </Link>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </article>
  )
}

function PriceCardSmall({ a, me }: { a: Alert; me: Me }) {
  return (
    <Link className="price-card" to={`/alerts/${a.id}`}>
      <span className="price-tag">Price move</span>
      <strong>{a.symbol}</strong>
      <Direction d={a.direction} />
      <span className="num">{pct(a.move)}</span>
      {a.linked_news.length > 0 && <span className="secondary">↳ {a.linked_news[0].minutes_after.toFixed(0)} min after news</span>}
      <span className="spacer" />
      <span className="secondary num">{fmtTime(a.ts, me.timezone)} {tzAbbr(me.timezone)}</span>
    </Link>
  )
}

type Row = { kind: 'news'; t: number; th: Thread } | { kind: 'price'; t: number; a: Alert }
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
  const col = (r: Row) => r.kind === 'news' ? columnOf(r.th.items[0]) : (r.a.direction > 0 ? 'pos' : 'neg')
  const groups = { pos: rows.filter((r) => col(r) === 'pos'), neg: rows.filter((r) => col(r) === 'neg'),
    neu: rows.filter((r) => col(r) === 'neu') }
  // counts are alerts (a thread of 3 updates counts 3)
  const count = (k: 'pos' | 'neg' | 'neu') => groups[k].reduce((acc, r) => acc + (r.kind === 'news' ? r.th.items.length : 0), 0)
  const list = (k: 'pos' | 'neg' | 'neu') => (
    <ol className="feed" aria-busy={loading}>
      {groups[k].map((r) => (
        <li key={r.kind === 'news' ? r.th.key : `p${r.a.id}`}>
          {r.kind === 'news' ? <NewsCard thread={r.th} me={me} fresh={r.th.items.some((x) => freshIds.has(x.id))} />
            : <PriceCardSmall a={r.a} me={me} />}
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
              aria-label={neuCollapsed ? 'Expand the Neutral column' : 'Collapse the Neutral column'}>{neuCollapsed ? '◂' : '▸'}</button>
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
    const t = setTimeout(() => setFilters((f) => (f.q === qInput.trim() ? f : { ...f, q: qInput.trim() })), 300)
    return () => clearTimeout(t)
  }, [qInput])

  useEffect(() => {
    if (layer === 'price') return
    let cancelled = false
    setLoading(true)
    setError('')
    const q = { sector: filters.sector, q: filters.q, limit: 80 }
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
    const allNews: NewsAlert[] = []
    for (const n of [...liveNews, ...news]) if (!seenN.has(n.id)) { seenN.add(n.id); allNews.push(n) }
    const out: Row[] = threadNews(allNews).map((th) => ({ kind: 'news' as const, t: th.items[0].created_at, th }))
    for (const a of [...livePrice, ...price]) if (!seenP.has(a.id)) { seenP.add(a.id); out.push({ kind: 'price', t: a.ts, a }) }
    return out.sort((x, y) => y.t - x.t)
  }, [liveNews, news, livePrice, price])

  async function loadMore() {
    const last = news[news.length - 1]
    if (!last) return
    const r = await api.news({ ...filters, before_id: last.id, limit: 80 })
    setNews((cur) => [...cur, ...r.items])
    setMore(r.more)
  }

  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }))
  const active = filters.sector || filters.event_type || filters.q

  return (
    <>
      <div className="feed-head">
        <h1>{VIEW_TITLE[layer]}</h1>
        <span className="live-pill"><span className={`dot ${conn === 'open' ? 'good' : 'warn'}`} aria-hidden="true" />
          {conn === 'open' ? 'Live' : 'Reconnecting…'}</span>
        <span className="spacer" />
        <div className="view-group">
          <span id="view-label" className="view-label">View</span>
          <div className="seg ctl-seg" role="group" aria-labelledby="view-label">
            {([['news', 'News'], ['both', 'News + price moves'], ['price', 'Price moves']] as const).map(([k, label]) => (
              <button key={k} type="button" aria-pressed={layer === k} onClick={() => setLayer(k)}>{label}</button>
            ))}
          </div>
        </div>
      </div>

      {layer === 'price' ? <Feed me={me} pushed={pushedPrice} conn={conn} embedded /> : (
        <>
          <form className="filters" role="search" onSubmit={(e) => e.preventDefault()}>
            <label className="sr-only" htmlFor="n-q">Search ticker, company or headline</label>
            <input id="n-q" className="ctl grow" type="search" placeholder="Search ticker, company or headline"
              list="n-symbols" value={qInput} onChange={(e) => setQInput(e.target.value)} />
            <datalist id="n-symbols">{me.symbols.map((s) => <option key={s} value={s} />)}</datalist>
            <label className="sr-only" htmlFor="n-sector">Sector</label>
            <select id="n-sector" className="ctl" value={filters.sector} onChange={(e) => set({ sector: e.target.value })}>
              <option value="">All sectors</option>
              {me.sectors.map((s) => <option key={s}>{s}</option>)}
            </select>
            <label className="sr-only" htmlFor="n-event">Event</label>
            <select id="n-event" className="ctl" value={filters.event_type} onChange={(e) => set({ event_type: e.target.value })}>
              <option value="">All events</option>
              {me.event_types.map((s) => <option key={s}>{s}</option>)}
            </select>
            {active && <button type="button" className="ctl btn-ctl" onClick={() => { setFilters(EMPTY); setQInput('') }}>Clear</button>}
          </form>
          {layer === 'both' && filters.event_type && (
            <p className="secondary" style={{ marginTop: -4 }}>Price moves are hidden while an event filter is set.</p>
          )}
          {error && <div className="empty error">{error}</div>}
          {!error && !loading && rows.length === 0 && (
            <div className="empty">{active ? 'No alerts match this search.'
              : 'No news alerts yet. They appear here as soon as a filing or headline is classified.'}</div>
          )}
          <Columns rows={rows} me={me} freshIds={freshIds} loading={loading} />
          {more && <div className="more"><button className="ctl btn-ctl" onClick={loadMore}>Load older news alerts</button></div>}
        </>
      )}
    </>
  )
}
