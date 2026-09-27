import { useEffect, useState } from 'react'
import { api, fmtDate, fmtTime, money, pct, tzAbbr, type AlertDetail as Detail, type Me } from '../api'
import { Direction } from './Feed'
import PriceChart from './PriceChart'

export default function AlertDetail({ me, id }: { me: Me; id: number }) {
  const [a, setA] = useState<Detail | null>(null)
  const [error, setError] = useState('')

  // App renders this with key={id}, so each alert gets fresh state
  useEffect(() => { api.alert(id).then(setA).catch((e) => setError(String(e))) }, [id])

  if (error) return <><a className="back" href="#/">← All alerts</a><div className="empty error">{error}</div></>
  if (!a) return <p className="muted">Loading…</p>
  const tz = me.timezone
  const mins = Math.round((a.ts - a.ref_ts) / 60)

  return (
    <>
      <a className="back" href="#/">← All alerts</a>
      <div className="detail-head">
        <h1>{a.symbol}</h1>
        <span className="secondary">{a.name}{a.sector ? ` · ${a.sector}` : ''}</span>
        <Direction d={a.direction} />
        <span className="muted">{fmtDate(a.ts, tz)} {fmtTime(a.ts, tz)} {tzAbbr(tz)}{a.mode === 'demo' ? ' (replayed)' : ''}</span>
      </div>

      <div className="kpis">
        <div className="kpi"><div className="label">Move in {mins} min</div><div className="value num">{pct(a.move)}</div></div>
        <div className="kpi"><div className="label">Price</div><div className="value num">{money(a.price, me.currency)}</div>
          <div className="muted num" style={{ fontSize: 13 }}>from {money(a.ref_price, me.currency)}</div></div>
        <div className="kpi"><div className="label">{me.index_name} over the same window</div><div className="value num">{pct(a.index_move)}</div></div>
        <div className="kpi"><div className="label">Correlation / beta vs {me.index_name}</div>
          <div className="value num">{a.corr !== null ? `${a.corr.toFixed(2)} / ${a.beta?.toFixed(2)}` : '—'}</div></div>
      </div>

      <section className="card" aria-labelledby="why">
        <h2 id="why">Why this alert passed</h2>
        <div className="reasons">
          <div className="reason">
            <h3><span className="check" aria-hidden="true">✓</span>Threshold</h3>
            <p>Moved {pct(a.move)} against the price {mins} minutes earlier; the threshold is ±{((me.params.move_threshold ?? 0) * 100).toFixed(1)}%.</p>
          </div>
          {a.reasons.ma && <div className="reason"><h3><span className="check" aria-hidden="true">✓</span>Moving-average trend</h3><p>{a.reasons.ma.text}</p></div>}
          {a.reasons.corr && <div className="reason"><h3><span className="check" aria-hidden="true">✓</span>Correlation with {me.index_name}</h3><p>{a.reasons.corr.text}</p></div>}
        </div>
      </section>

      <section className="card" aria-labelledby="ctx">
        <h2 id="ctx">Price context</h2>
        <PriceChart symbol={a.symbol} indexName={me.index_name} prices={a.context.prices} index={a.context.index}
          refTs={a.ref_ts} alertTs={a.ts} start={a.context.start} end={a.context.end} tz={tz} currency={me.currency} />
      </section>

      <section className="card" aria-labelledby="news">
        <h2 id="news">Matching news</h2>
        {a.news.length === 0 ? (
          <p className="muted" style={{ margin: 0 }}>
            {a.mode === 'demo'
              ? 'None: replayed data has no archived news, so demo alerts never have news items.'
              : 'No matching NSE announcement or BusinessLine headline was found when this alert fired.'}
          </p>
        ) : (
          <ul className="news">
            {a.news.map((n, i) => (
              <li key={i}>
                <a href={n.url} target="_blank" rel="noopener noreferrer nofollow">{n.headline}</a>
                <span className="meta">{n.source}{n.published ? ` · ${new Date(n.published).toLocaleString('en-GB', { timeZone: tz })}` : ''}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="muted" style={{ fontSize: 12, margin: '10px 0 0' }}>Headline, source and link only; articles open on the publisher's site.</p>
      </section>
    </>
  )
}
