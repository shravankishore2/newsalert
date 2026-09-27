import { ago, fmtTime, tzAbbr, type Me, type Status } from '../api'
import type { Conn } from '../stream'

function Cell({ label, dot, value, detail }: { label: string; dot: string; value: string; detail?: string }) {
  return (
    <div className="status-cell">
      <div className="label">{label}</div>
      <div className="value"><span className={`dot ${dot}`} aria-hidden="true" />{value}</div>
      {detail && <div className="detail" title={detail}>{detail}</div>}
    </div>
  )
}

export default function StatusBar({ me, status, conn }: { me: Me; status: Status | null; conn: Conn }) {
  const tz = me.timezone
  const now = status?.server_time ?? 0
  const demo = me.mode === 'demo'

  // market
  const m = status?.market
  const market = !m ? <Cell label="Market" dot="idle" value="…" /> : (
    <Cell label={demo ? 'Market (replayed)' : 'Market'} dot={m.open ? 'good' : 'idle'}
      value={m.open ? 'Open' : 'Closed'}
      detail={m.open ? (m.now_ts ? `${fmtTime(m.now_ts, tz)} ${tzAbbr(tz)}` : undefined)
        : m.next_open ? `Opens ${fmtTime(Date.parse(m.next_open) / 1000, tz, true)} ${tzAbbr(tz)}`
          : demo ? 'Between replayed sessions' : undefined} />
  )

  // last price cycle
  const c = status?.cycle
  const cycle = !c ? <Cell label="Last price cycle" dot="idle" value="None yet" /> : (
    <Cell label="Last price cycle" dot={c.value.error ? 'bad' : c.value.failed ? 'warn' : 'good'}
      value={demo && c.value.sim_ts ? `${fmtTime(c.value.sim_ts, tz)} ${tzAbbr(tz)} (replayed)` : ago(now - c.value.at)}
      detail={c.value.error ? c.value.error
        : `${c.value.ok} prices${c.value.failed ? `, ${c.value.failed} missing` : ''}${c.value.alerts ? `, ${c.value.alerts} alert(s)` : ''}`} />
  )

  // token
  const t = status?.token?.value
  let token
  if (!t) token = <Cell label="Dhan token" dot="idle" value="Unknown" />
  else if (t.state === 'not used') token = <Cell label="Dhan token" dot="idle" value="Not used" detail="Demo replays stored data" />
  else if (t.state === 'valid' && t.expires_at) {
    const left = (Date.parse(t.expires_at) / 1000 - now) / 3600
    token = <Cell label="Dhan token" dot={left < 2 ? 'warn' : 'good'} value={`Valid, ${left.toFixed(1)} h left`}
      detail={t.auto_refresh ? 'Auto-refresh via TOTP' : 'Pasted token: no auto-refresh'} />
  } else token = <Cell label="Dhan token" dot="bad" value={t.state === 'error' ? 'Error' : 'Missing'} detail={t.error ?? undefined} />

  // feeds
  const f = status?.feeds?.value
  let feeds
  if (!f || Object.keys(f).length === 0) feeds = <Cell label="News feeds" dot="idle" value="Not checked yet" />
  else if (f._note) feeds = <Cell label="News feeds" dot="idle" value="Not used" detail={f._note.note} />
  else {
    const entries = Object.entries(f)
    const bad = entries.filter(([, v]) => !v.ok)
    feeds = <Cell label="News feeds" dot={bad.length ? (bad.length === entries.length ? 'bad' : 'warn') : 'good'}
      value={bad.length ? `${bad.length} of ${entries.length} failing` : `${entries.length} healthy`}
      detail={bad.length ? bad.map(([k, v]) => `${k}: ${v.error}`).join('; ') : entries.map(([k]) => k).join(', ')} />
  }

  const push = <Cell label="Push channel" dot={conn === 'open' ? 'good' : 'warn'}
    value={conn === 'open' ? 'Connected' : conn === 'connecting' ? 'Connecting…' : 'Reconnecting…'} />

  return <section className="statusbar" aria-label="System status">{market}{cycle}{token}{feeds}{push}</section>
}
