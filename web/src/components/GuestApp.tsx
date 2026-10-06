import { useEffect, useMemo, useState } from 'react'
import { ago, fmtDate, fmtDuration, fmtTime, pct, tzAbbr } from '../api'
import { guestApi, type GuestMeta, type GuestNews, type GuestStatus, type GuestStock } from '../guest'
import ThemeToggle from './ThemeToggle'
import { threadNews, type Thread } from '../news'

type Col = 'pos' | 'neg' | 'neu'
const TITLES: Record<Col, string> = { pos: 'Positive', neg: 'Negative', neu: 'Neutral / watch' }
const REPO = 'https://github.com/shravankishore2/newsalert'

const primary = (n: GuestNews) => n.stocks.find((s) => s.relation === 'direct') ?? n.stocks[0]
const columnOf = (n: GuestNews): Col => {
  const d = primary(n)?.direction
  return d === 'up' ? 'pos' : d === 'down' ? 'neg' : 'neu'
}

function Impact({ s }: { s: GuestStock | undefined }) {
  const d = s?.direction
  const strength = s?.strength ? s.strength[0].toUpperCase() + s.strength.slice(1) : null
  return (
    <span className={`impact impact-${d ?? 'none'}`} title="Expected direction and impact strength">
      <span aria-hidden="true">{d === 'up' ? '▲' : d === 'down' ? '▼' : '•'}</span>
      {d === 'up' ? 'Up' : d === 'down' ? 'Down' : 'Neutral'}
      {strength && <> · {strength}<span className="impact-word"> impact</span></>}
    </span>
  )
}

/** What happened to the price since the alert, as %: the event study once it has run, else live. */
function moves(s: GuestStock | undefined, index: string): string | null {
  if (!s) return null
  const pairs: [string, number | null, number | null][] = [
    ['to close', s.ret_close, s.abn_close], ['1 h', s.ret_1h, s.abn_1h], ['15 min', s.ret_15m, s.abn_15m]]
  const done = pairs.find(([, r]) => r !== null)
  if (done) return `${s.ticker} ${done[0]} after the alert: ${pct(done[1])}${done[2] !== null ? ` (${pct(done[2])} vs ${index})` : ''}`
  if (s.move_since_alert !== null) return `${s.ticker} since the alert: ${pct(s.move_since_alert)}`
  return null
}

function Card({ th, meta, fresh }: { th: Thread<GuestNews>; meta: GuestMeta; fresh: boolean }) {
  const n = th.items[0]
  const tz = meta.timezone
  const p = primary(n)
  const others = n.stocks.filter((s) => s !== p)
  const move = moves(p, meta.index_name)
  return (
    <article className={`news-card tone-${columnOf(n)}${fresh ? ' fresh' : ''}`}>
      <div className="nc-top">
        <span className="event-badge">{n.event_type}</span>
        <Impact s={p} />
      </div>
      <div className="nc-company">{p?.name || p?.ticker || 'Market'}{p && <span className="nc-ticker"> · {p.ticker}</span>}</div>
      <p className="nc-summary">{n.reasoning}</p>
      {move && <p className="nc-move num">{move}</p>}
      {others.length > 0 && (
        <div className="chips" aria-label="Other affected stocks">
          {others.map((s) => (
            <span key={`${s.ticker}-${s.relation}`} className={`chip ${s.direction ?? 'none'}`}>
              <span aria-hidden="true">{s.direction === 'up' ? '▲' : s.direction === 'down' ? '▼' : '•'}</span>
              <strong>{s.ticker}</strong><span className="chip-meta">{s.relation}</span>
            </span>
          ))}
        </div>
      )}
      <footer className="nc-foot">
        <span className="num">{fmtTime(n.created_at, tz)} {tzAbbr(tz)}, {fmtDate(n.created_at, tz)}</span>
        {n.latency_s !== null && <span>alerted {fmtDuration(n.latency_s)} after publication</span>}
        <span>{n.classifier === 'rules' ? 'rule-based' : n.confidence !== null ? `${Math.round(n.confidence * 100)}% confidence` : ''}</span>
        {n.linked_price_moves.length > 0 && (
          <span>↳ {n.linked_price_moves.map((x) => `${x.symbol} ${pct(x.move)}`).join(', ')}</span>
        )}
        {n.url
          ? <a href={n.url} target="_blank" rel="noopener noreferrer">Read at {n.source_name} ↗</a>
          : <span>{n.source_name}</span>}
        {th.items.length > 1 && <span>{th.items.length} updates within the hour</span>}
      </footer>
    </article>
  )
}

function Market({ st, tz }: { st: GuestStatus | null; tz: string }) {
  const m = st?.market
  const detail = !m ? '' : !m.open && m.next_open ? `opens ${fmtTime(Date.parse(m.next_open) / 1000, tz, true)} ${tzAbbr(tz)}` : ''
  const nv = st?.news
  return (
    <section className="statusbar2" aria-label="Market status">
      <div className="market-state">
        <span className={`dot big ${m?.open ? 'good' : 'idle'}`} aria-hidden="true" />
        <strong>{!m ? 'Market …' : `Market ${m.open ? 'open' : 'closed'}`}</strong>
        {detail && <span className="secondary num">· {detail}</span>}
      </div>
      {nv && st && (
        <span className="secondary">
          News feeds {nv.feeds_ok}/{nv.feeds} OK{nv.last_poll_at ? `, polled ${ago(st.server_time - nv.last_poll_at)}` : ''}
          {nv.alerts_today !== null ? ` · ${nv.alerts_today} alerts today` : ''}
        </span>
      )}
    </section>
  )
}

export default function GuestApp() {
  const [meta, setMeta] = useState<GuestMeta | null>(null)
  const [status, setStatus] = useState<GuestStatus | null>(null)
  const [news, setNews] = useState<GuestNews[]>([])
  const [pushed, setPushed] = useState<GuestNews[]>([])
  const [more, setMore] = useState(false)
  const [eventType, setEventType] = useState('')
  const [tab, setTab] = useState<Col>('pos')
  const [error, setError] = useState('')
  const [live, setLive] = useState(false)

  useEffect(() => {
    document.title = 'QuantRadar · read-only demo'
    guestApi.meta().then((m) => { setMeta(m); setStatus(m.status) }, (e) => setError(String(e.message ?? e)))
  }, [])

  useEffect(() => {
    if (!meta) return
    let cancelled = false
    guestApi.news({ event_type: eventType, limit: 80 }).then((r) => {
      if (!cancelled) { setNews(r.items); setMore(r.more) }
    }, (e) => !cancelled && setError(String(e.message ?? e)))
    return () => { cancelled = true }
  }, [meta, eventType])

  useEffect(() => {
    if (!meta) return
    const es = new EventSource(guestApi.streamUrl())
    es.onopen = () => setLive(true)
    es.onerror = () => setLive(false)
    es.addEventListener('status', (e) => setStatus(JSON.parse((e as MessageEvent).data)))
    es.addEventListener('news', (e) => {
      const n = JSON.parse((e as MessageEvent).data) as GuestNews
      setPushed((p) => [n, ...p.filter((x) => x.id !== n.id)].slice(0, 300))
    })
    return () => es.close()
  }, [meta])

  const baseMax = useMemo(() => Math.max(0, ...news.map((n) => n.id)), [news])
  const fresh = useMemo(() => pushed.filter((n) => n.id > baseMax && (!eventType || n.event_type === eventType)),
    [pushed, baseMax, eventType])
  const all = useMemo(() => [...fresh, ...news].sort((a, b) => b.created_at - a.created_at), [fresh, news])
  const freshIds = useMemo(() => new Set(fresh.map((n) => n.id)), [fresh])

  if (error) return <div className="login"><p>{error}</p></div>
  if (!meta) return <div className="login"><p className="muted">Loading…</p></div>

  const groups: Record<Col, Thread<GuestNews>[]> = { pos: [], neg: [], neu: [] }
  for (const th of threadNews(all)) groups[columnOf(th.items[0])].push(th)
  const count = (k: Col) => groups[k].reduce((a, th) => a + th.items.length, 0)
  const loadMore = async () => {
    const last = news[news.length - 1]
    if (!last) return
    const r = await guestApi.news({ event_type: eventType, before_id: last.id, limit: 80 })
    setNews((cur) => [...cur, ...r.items])
    setMore(r.more)
  }

  return (
    <div className="app">
      <div className="replay-banner guest-banner" role="note" aria-label="Demo notice">
        <span className="replay-tag">READ-ONLY DEMO</span>
        <span>{meta.banner}</span>
      </div>
      <header className="topbar">
        <div className="brand"><img src="/favicon.svg" alt="" /> QuantRadar</div>
        <div className="spacer" />
        <span className="muted" style={{ fontSize: 13 }}>{meta.dataset}</span>
        <a className="btn" href={REPO} target="_blank" rel="noopener noreferrer">How it works ↗</a>
        <ThemeToggle />
      </header>
      <Market st={status} tz={meta.timezone} />
      <main className="main wide">
        <div className="feed-head">
          <h1>News alerts</h1>
          <span className="live-pill"><span className={`dot ${live ? 'good' : 'warn'}`} aria-hidden="true" />
            {live ? 'Live' : 'Connecting…'}</span>
          <span className="spacer" />
          <label className="sr-only" htmlFor="g-event">Event</label>
          <select id="g-event" className="ctl" value={eventType} onChange={(e) => setEventType(e.target.value)}>
            <option value="">All events</option>
            {meta.event_types.map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
        {all.length === 0 && <div className="empty">No news alerts yet. They appear here as soon as a filing or article is classified.</div>}
        <div className="col-tabs" role="tablist" aria-label="News columns">
          {(['pos', 'neg', 'neu'] as const).map((k) => (
            <button key={k} role="tab" aria-selected={tab === k} className={`col-tab t-${k}`} onClick={() => setTab(k)}>
              {TITLES[k]} <span className="count">{count(k)}</span>
            </button>
          ))}
        </div>
        <div className="columns" data-tab={tab} aria-live="polite">
          {(['pos', 'neg', 'neu'] as const).map((k) => (
            <section key={k} className={`col col-${k}`} aria-labelledby={`gh-${k}`}>
              <h2 id={`gh-${k}`} className="col-head">
                {k !== 'neu' && <span className="col-dot" aria-hidden="true">{k === 'pos' ? '▲' : '▼'}</span>}
                {TITLES[k]} <span className="count">{count(k)}</span>
              </h2>
              <ol className="feed">
                {groups[k].map((th) => <li key={th.key}><Card th={th} meta={meta} fresh={th.items.some((x) => freshIds.has(x.id))} /></li>)}
                {groups[k].length === 0 && <li className="col-empty">Nothing here yet.</li>}
              </ol>
            </section>
          ))}
        </div>
        {more && <div className="more"><button className="ctl btn-ctl" onClick={loadMore}>Load older news alerts</button></div>}
      </main>
    </div>
  )
}
