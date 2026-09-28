import { useEffect, useState } from 'react'
import { boards, fmtDate, fmtTime, pct, tzAbbr, type ActionItem, type Me, type ResultCard } from '../api'
import Gauge from './Gauge'

const KIND_LABEL: Record<string, string> = {
  dividend: 'Dividend', bonus: 'Bonus issue', split: 'Stock split', buyback: 'Buyback', 'record date': 'Record date',
}

function fmtCr(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return `₹${v.toLocaleString('en-IN', { maximumFractionDigits: v >= 100 ? 0 : 1 })} cr`
}
function fmtPctPlain(v: number | null | undefined) {
  return v === null || v === undefined ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}%`
}
function fmtNum(v: number | null | undefined, digits = 2) {
  return v === null || v === undefined ? '—' : v.toLocaleString('en-IN', { maximumFractionDigits: digits })
}

function ResultCardView({ c, me }: { c: ResultCard; me: Me }) {
  const tz = me.timezone
  const rx = c.reaction
  const gaugeCaption = c.gauge.mode === 'expectations' ? 'Earnings surprise vs expectations' : 'Stock reaction vs NIFTY 50 (not an earnings verdict)'
  return (
    <article className="result-card">
      <header>
        <div>
          <h3>{c.name || c.ticker} <span className="ticker-tag">{c.ticker}</span></h3>
          <div className="muted" style={{ fontSize: 13 }}>{c.sector} · {fmtDate(c.first_alert_at, tz)} {fmtTime(c.first_alert_at, tz)} {tzAbbr(tz)}</div>
        </div>
      </header>
      <div className="result-body">
        <Gauge value={c.gauge.value} min={c.gauge.min} max={c.gauge.max} label={c.verdict ?? c.gauge.label} caption={gaugeCaption} />
        <div className="result-text">
          <p className="briefing">{c.briefing}</p>
          <p className="basis"><span className="basis-tag">Basis</span> {c.basis_label}</p>
          {c.expectations ? (
            <table className="data mini">
              <thead><tr><th></th><th>Expected</th><th>Actual</th><th>Surprise</th></tr></thead>
              <tbody className="num">
                {(c.expectations.eps_estimate != null || c.expectations.eps_actual != null) &&
                  <tr><td>EPS (₹)</td><td>{fmtNum(c.expectations.eps_estimate)}</td><td>{fmtNum(c.expectations.eps_actual)}</td><td>{fmtPctPlain(c.expectations.eps_surprise_pct)}</td></tr>}
                {(c.expectations.revenue_estimate != null || c.expectations.revenue_actual != null) &&
                  <tr><td>Revenue</td><td>{fmtNum(c.expectations.revenue_estimate, 0)}</td><td>{fmtNum(c.expectations.revenue_actual, 0)}</td><td>{fmtPctPlain(c.expectations.revenue_surprise_pct)}</td></tr>}
              </tbody>
            </table>
          ) : c.figures ? (
            <table className="data mini">
              <thead><tr><th>{c.figures.period ?? 'This quarter'}</th><th>Actual</th><th>vs same quarter last year</th></tr></thead>
              <tbody className="num">
                {c.figures.revenue_cr != null || c.figures.revenue_yoy_pct != null ? <tr><td>Revenue</td><td>{fmtCr(c.figures.revenue_cr)}</td><td>{fmtPctPlain(c.figures.revenue_yoy_pct)}</td></tr> : null}
                {c.figures.profit_cr != null || c.figures.profit_yoy_pct != null ? <tr><td>Net profit</td><td>{fmtCr(c.figures.profit_cr)}</td><td>{fmtPctPlain(c.figures.profit_yoy_pct)}</td></tr> : null}
                {c.figures.eps != null || c.figures.eps_yoy_pct != null ? <tr><td>EPS</td><td>{c.figures.eps != null ? `₹${fmtNum(c.figures.eps)}` : '—'}</td><td>{fmtPctPlain(c.figures.eps_yoy_pct)}</td></tr> : null}
              </tbody>
            </table>
          ) : null}
          <div className="reaction">
            <span className="basis-tag">Reaction vs {me.index_name}</span>
            {rx ? (
              <span className="num">
                +15 min <strong>{pct(rx.abn_15m)}</strong> · +1 h <strong>{pct(rx.abn_1h)}</strong> · close <strong>{pct(rx.abn_close)}</strong>
                <span className="muted"> {rx.provisional ? '(so far, live)' : '(final)'}{rx.t0_rule === 'next open' ? ' · from next open' : ''}</span>
              </span>
            ) : <span className="muted">not available</span>}
          </div>
          <div className="sources">
            {c.sources.map((s, i) => (
              <a key={i} href={s.url} target="_blank" rel="noopener noreferrer nofollow">
                {s.source === 'nse' ? 'NSE filing' : 'BusinessLine'} ↗</a>
            ))}
          </div>
        </div>
      </div>
    </article>
  )
}

export default function Board({ me }: { me: Me }) {
  const [results, setResults] = useState<{ items: ResultCard[]; expectations_configured: boolean } | null>(null)
  const [actions, setActions] = useState<ActionItem[] | null>(null)
  const [days, setDays] = useState(7)
  const [error, setError] = useState('')
  useEffect(() => {
    boards.results(days).then(setResults).catch((e) => setError(String(e)))
    boards.actions(30).then((r) => setActions(r.items)).catch((e) => setError(String(e)))
  }, [days])
  const tz = me.timezone
  const sortedActions = actions ? [...actions].sort((a, b) =>
    Number(b.upcoming) - Number(a.upcoming) || (a.upcoming ? (a.action_date ?? '').localeCompare(b.action_date ?? '')
      : b.filed_at - a.filed_at)) : null

  return (
    <>
      <div className="feed-head">
        <h1>Company results</h1>
        <span className="spacer" />
        <label className="muted" htmlFor="b-days" style={{ fontSize: 13 }}>Show last</label>
        <select id="b-days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>1 day</option><option value={7}>7 days</option><option value={30}>30 days</option>
        </select>
      </div>
      {error && <div className="empty error">{error}</div>}
      {results && !results.expectations_configured && (
        <p className="notice">
          <strong>No analyst expectations.</strong> Consensus estimates are usually paid data. A free source with
          personal-use terms (Finnhub) is supported but not configured. Until it is, cards show actual vs the same quarter
          last year when a BusinessLine headline states the figures, and otherwise only the stock's reaction. Each card
          says which.
        </p>
      )}
      <section aria-labelledby="earn">
        <h2 id="earn" className="section-title">Earnings results <span className="count">{results?.items.length ?? '…'}</span></h2>
        {results && results.items.length === 0 && <div className="empty">No companies reported results in this period.</div>}
        <div className="result-grid">
          {results?.items.map((c) => <ResultCardView key={`${c.ticker}-${c.date}`} c={c} me={me} />)}
        </div>
      </section>
      <section aria-labelledby="acts" style={{ marginTop: 28 }}>
        <h2 id="acts" className="section-title">Corporate actions <span className="count">{actions?.length ?? '…'}</span></h2>
        <p className="muted" style={{ marginTop: -6 }}>Dividends, bonus issues, splits, buybacks and record dates from NSE filings
          (last 30 days), classified by rules. Dates are shown only when the filing states one.</p>
        {sortedActions && sortedActions.length === 0 && <div className="empty">No corporate actions filed in the last 30 days.</div>}
        {sortedActions && sortedActions.length > 0 && (
          <div className="actions-list">
            {sortedActions.map((a, i) => (
              <a key={i} className={`action-row${a.upcoming ? ' upcoming' : ''}`} href={a.url} target="_blank" rel="noopener noreferrer nofollow">
                <span className="action-kind">{KIND_LABEL[a.kind] ?? a.kind}</span>
                <span className="action-co"><strong>{a.name || a.ticker}</strong> <span className="ticker-tag">{a.ticker}</span></span>
                <span className="action-date num">{a.action_date ? `${a.upcoming ? 'Upcoming · ' : ''}${fmtDate(new Date(a.action_date + 'T12:00:00+05:30').getTime() / 1000, tz)}` : 'date not stated'}</span>
                <span className="muted num action-filed">filed {fmtDate(a.filed_at, tz)} {fmtTime(a.filed_at, tz)}</span>
              </a>
            ))}
          </div>
        )}
      </section>
    </>
  )
}
