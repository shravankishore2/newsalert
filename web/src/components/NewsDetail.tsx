import { useEffect, useState } from 'react'
import { api, fmtDate, fmtDuration, fmtTime, pct, SOURCE_LABEL, tzAbbr, type Me, type NewsDetail as Detail } from '../api'
import { EventBadge, StockChip } from './NewsFeed'
import PriceChart from './PriceChart'

function Ret({ v }: { v: number | null }) {
  return <td className="num">{v === null ? '—' : pct(v)}</td>
}

export default function NewsDetail({ me, id }: { me: Me; id: number }) {
  const [n, setN] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  // App renders this with key={id}, so each alert gets fresh state
  useEffect(() => { api.newsItem(id).then(setN).catch((e) => setError(String(e))) }, [id])

  if (error) return <><a className="back" href="#/">← All alerts</a><div className="empty error">{error}</div></>
  if (!n) return <p className="muted">Loading…</p>
  const tz = me.timezone
  const evaluated = n.stocks.some((s) => s.evaluated_at)

  return (
    <>
      <a className="back" href="#/">← All alerts</a>
      <div className="news-top" style={{ marginBottom: 6 }}>
        <EventBadge type={n.event_type} />
        <span className="muted">{SOURCE_LABEL[n.source] ?? n.source}</span>
        {n.mode === 'demo' && <span className="replay-tag" style={{ color: 'var(--replay-ink)' }}>REPLAYED</span>}
      </div>
      <h1 className="news-title">{n.headline}</h1>
      <p className="secondary" style={{ marginTop: 0 }}>
        <a href={n.url} target="_blank" rel="noopener noreferrer nofollow">
          {n.source === 'nse' ? 'Open the filing on NSE' : 'Read on BusinessLine'} ↗</a>
        {' · '}Published {n.published_at ? `${fmtDate(n.published_at, tz)} ${fmtTime(n.published_at, tz)} ${tzAbbr(tz)}` : 'time unknown'}
        {' · '}alerted {fmtDuration(n.latency_s)} later
      </p>

      <section className="card" aria-labelledby="cls">
        <h2 id="cls">Classification</h2>
        <p className="secondary" style={{ marginTop: 0 }}>
          {n.classifier === 'gemini'
            ? <>Classified by Gemini from the headline and feed summary; confidence {n.confidence !== null ? `${Math.round(n.confidence * 100)}%` : '—'}.</>
            : <>NSE filing classified by local rules from its subject (NSE content is never sent to an LLM or stored). Direction is only given where the event type implies one.</>}
        </p>
        <table className="data stocks">
          <thead><tr><th>Stock</th><th>Relation</th><th>Expected</th><th>Strength</th><th>Reason</th></tr></thead>
          <tbody>
            {n.stocks.map((s) => (
              <tr key={`${s.ticker}-${s.relation}`}>
                <td><StockChip s={s} /><div className="muted" style={{ fontSize: 12 }}>{s.name}{s.sector ? ` · ${s.sector}` : ''}</div></td>
                <td>{s.relation}</td><td>{s.direction ?? 'not inferred'}</td><td>{s.strength ?? '—'}</td>
                <td style={{ textAlign: 'left' }}>{s.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card" aria-labelledby="ev">
        <h2 id="ev">What happened next (return vs {me.index_name})</h2>
        {!evaluated ? (
          <p className="muted" style={{ margin: 0 }}>Evaluated after the session closes. Returns are measured from the alert
            time (or the next open, if it came outside market hours) at +15 min, +1 h and the close.</p>
        ) : (
          <table className="data">
            <thead><tr><th>Stock</th><th>From</th><th>+15 min</th><th>+1 h</th><th>Close</th><th>Call</th></tr></thead>
            <tbody>
              {n.stocks.map((s) => {
                const hit = s.direction && s.abn_close !== null && s.abn_close !== 0
                  ? ((s.direction === 'up') === (s.abn_close > 0) ? 'hit' : 'miss') : '—'
                return (
                  <tr key={`${s.ticker}-${s.relation}-ev`}>
                    <td>{s.ticker}</td>
                    <td>{s.t0 ? `${fmtTime(s.t0, tz)} (${s.t0_rule})` : s.eval_note ?? '—'}</td>
                    <Ret v={s.abn_15m} /><Ret v={s.abn_1h} /><Ret v={s.abn_close} />
                    <td>{hit}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </section>

      {n.linked_price_alerts.length > 0 && (
        <section className="card" aria-labelledby="lk">
          <h2 id="lk">Linked price moves (within 60 min)</h2>
          <ul className="news">
            {n.linked_price_alerts.map((p) => (
              <li key={p.id}><a href={`#/alerts/${p.id}`}>{p.symbol} {pct(p.move)}</a>
                <span className="meta">{p.minutes_after.toFixed(0)} min after the news alert · {fmtTime(p.ts, tz)} {tzAbbr(tz)}</span></li>
            ))}
          </ul>
        </section>
      )}

      {n.context && (
        <section className="card" aria-labelledby="ctx">
          <h2 id="ctx">Price context: {n.context.symbol}</h2>
          <PriceChart symbol={n.context.symbol} indexName={me.index_name} prices={n.context.prices} index={n.context.index}
            refTs={n.context.marker} alertTs={n.context.marker} start={n.context.start} end={n.context.end}
            tz={tz} currency={me.currency} markers={[{ ts: n.context.marker, label: 'News alert' }]} baseLabel="the news alert" />
        </section>
      )}
      <p className="muted" style={{ fontSize: 12 }}>Headline, source and link only; the article opens on the publisher's site.</p>
    </>
  )
}
