import { useEffect, useMemo, useState } from 'react'
import { api, fmtDate, fmtTime, pct, tzAbbr, type Alert, type Me } from '../api'
import type { Conn } from '../stream'

type Filters = { symbol: string; sector: string; direction: '' | 'up' | 'down'; q: string }
const EMPTY: Filters = { symbol: '', sector: '', direction: '', q: '' }

function matches(a: Alert, f: Filters) {
  if (f.symbol && a.symbol !== f.symbol.toUpperCase()) return false
  if (f.sector && a.sector !== f.sector) return false
  if (f.direction && (a.direction > 0 ? 'up' : 'down') !== f.direction) return false
  if (f.q) {
    const q = f.q.toLowerCase()
    const hay = [a.symbol, a.name, ...a.news.map((n) => n.headline)].join(' ').toLowerCase()
    if (!hay.includes(q)) return false
  }
  return true
}

export function Direction({ d }: { d: 1 | -1 }) {
  return d > 0
    ? <span className="dir up"><span className="arrow" aria-hidden="true">▲</span>Up</span>
    : <span className="dir down"><span className="arrow" aria-hidden="true">▼</span>Down</span>
}

/** One-line "why it passed" summary from the stored filter numbers. */
function why(a: Alert, indexName: string) {
  const parts: React.ReactNode[] = []
  const r = a.reasons
  if (r.ma) {
    parts.push(<span key="ma"><span className="check" aria-hidden="true">✓</span>
      MA: fast SMA moved {pct(r.ma.fast_move as number)}, {a.direction > 0 ? 'above' : 'below'} slow SMA</span>)
  }
  if (r.corr) {
    parts.push(<span key="corr"><span className="check" aria-hidden="true">✓</span>
      {a.residual !== null
        ? <>Corr {a.corr?.toFixed(2)}: {pct(a.residual)} after removing {indexName}</>
        : <>Corr {a.corr?.toFixed(2)} (low): stock-specific; {indexName} {pct(a.index_move)}</>}
    </span>)
  }
  if (!parts.length) parts.push(<span key="none" className="muted">Filters disabled</span>)
  return parts
}

export default function Feed({ me, pushed, conn }: { me: Me; pushed: Alert[]; conn: Conn }) {
  const [filters, setFilters] = useState<Filters>(EMPTY)
  const [qInput, setQInput] = useState('')
  const [items, setItems] = useState<Alert[]>([])
  const [more, setMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [baseline, setBaseline] = useState(0) // highest id known when the current filter was applied

  // debounce search typing
  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: qInput.trim() })), 300)
    return () => clearTimeout(t)
  }, [qInput])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    api.alerts({ ...filters, limit: 50 }).then((r) => {
      if (cancelled) return
      setItems(r.items)
      setMore(r.more)
      setBaseline(Math.max(0, ...r.items.map((a) => a.id), ...pushed.map((a) => a.id)))
    }).catch((e) => !cancelled && setError(String(e))).finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters])

  // alerts pushed after the list was loaded, matching the current filters, go on top
  const live = useMemo(() => pushed.filter((a) => a.id > baseline && matches(a, filters)), [pushed, filters, baseline])
  const fresh = useMemo(() => new Set(live.map((a) => a.id)), [live])
  const shown = useMemo(() => {
    const seen = new Set<number>()
    return [...live, ...items].filter((a) => (seen.has(a.id) ? false : (seen.add(a.id), true)))
  }, [live, items])

  async function loadMore() {
    const last = items[items.length - 1]
    if (!last) return
    const r = await api.alerts({ ...filters, before_id: last.id, limit: 50 })
    setItems((cur) => [...cur, ...r.items])
    setMore(r.more)
  }

  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }))
  const active = filters.symbol || filters.sector || filters.direction || filters.q
  const tz = me.timezone

  return (
    <>
      <div className="feed-head">
        <h2 style={{ margin: 0, fontSize: 17 }}>Price-move alerts</h2>
        <span className="live-pill"><span className={`dot ${conn === 'open' ? 'good' : 'warn'}`} aria-hidden="true" />
          {conn === 'open' ? 'Live: new alerts appear automatically' : 'Reconnecting…'}</span>
      </div>

      <form className="filters" role="search" onSubmit={(e) => e.preventDefault()}>
        <div className="field">
          <label htmlFor="f-sym">Ticker</label>
          <input id="f-sym" list="symbols" placeholder="Any" value={filters.symbol} size={10}
            onChange={(e) => set({ symbol: e.target.value.toUpperCase() })} />
          <datalist id="symbols">{me.symbols.map((s) => <option key={s} value={s} />)}</datalist>
        </div>
        <div className="field">
          <label htmlFor="f-sector">Sector</label>
          <select id="f-sector" value={filters.sector} onChange={(e) => set({ sector: e.target.value })}>
            <option value="">All sectors</option>
            {me.sectors.map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
        <div className="field">
          <span id="f-dir" style={{ fontSize: 12, color: 'var(--text-muted)' }}>Direction</span>
          <div className="seg" role="group" aria-labelledby="f-dir">
            {(['', 'up', 'down'] as const).map((d) => (
              <button key={d} type="button" aria-pressed={filters.direction === d} onClick={() => set({ direction: d })}>
                {d === '' ? 'Both' : d === 'up' ? '▲ Up' : '▼ Down'}
              </button>
            ))}
          </div>
        </div>
        <div className="field grow">
          <label htmlFor="f-q">Search past alerts</label>
          <input id="f-q" type="search" placeholder="Ticker, company or headline" value={qInput}
            onChange={(e) => setQInput(e.target.value)} />
        </div>
        {active && <button type="button" className="btn" onClick={() => { setFilters(EMPTY); setQInput('') }}>Clear</button>}
      </form>

      {error && <div className="empty error">{error}</div>}
      {!error && !loading && shown.length === 0 && (
        <div className="empty">{active ? 'No alerts match these filters.' : 'No alerts yet. New ones appear here as soon as they fire.'}</div>
      )}
      <ol className="feed" aria-live="polite" aria-busy={loading}>
        {shown.map((a) => (
          <li key={a.id}>
            <a className={`alert-card${fresh.has(a.id) ? ' fresh' : ''}`} href={`#/alerts/${a.id}`}>
              <div style={{ minWidth: 0 }}>
                <div className="ticker">{a.symbol}</div>
                <div className="company">{a.name}{a.sector ? ` · ${a.sector}` : ''}</div>
              </div>
              <div>
                <Direction d={a.direction} />
                <div className="num" style={{ fontSize: 18, fontWeight: 650 }}>{pct(a.move)}</div>
              </div>
              <div className="why">{why(a, me.index_name)}</div>
              <div className="when num">
                {fmtTime(a.ts, tz)} {tzAbbr(tz)}
                <div className="muted">{fmtDate(a.ts, tz)}</div>
                {a.linked_news.length > 0 && <div><a href={`#/news/${a.linked_news[0].news_alert_id}`}
                  onClick={(e) => e.stopPropagation()}>↳ after news</a></div>}
              </div>
            </a>
          </li>
        ))}
      </ol>
      {more && <div className="more"><button className="btn" onClick={loadMore}>Load older alerts</button></div>}
    </>
  )
}
