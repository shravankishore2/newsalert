import { useEffect, useRef, useState } from 'react'
import { ago, fmtTime, tzAbbr, type Me, type Status } from '../api'
import type { Conn } from '../stream'

type Level = 'good' | 'warn' | 'bad' | 'idle'
type Check = { label: string; level: Level; value: string; detail?: string }

function checks(me: Me, status: Status | null, conn: Conn): Check[] {
  const tz = me.timezone
  const now = status?.server_time ?? 0
  const demo = me.mode === 'demo'
  const out: Check[] = []

  const c = status?.cycle
  out.push(!c ? { label: 'Last price cycle', level: 'idle', value: 'None yet' } : {
    label: 'Last price cycle', level: c.value.error ? 'bad' : c.value.failed ? 'warn' : 'good',
    value: demo && c.value.sim_ts ? `${fmtTime(c.value.sim_ts, tz)} ${tzAbbr(tz)} (replayed)` : ago(now - c.value.at),
    detail: c.value.error ? c.value.error
      : `${c.value.ok} prices${c.value.failed ? `, ${c.value.failed} missing` : ''}${c.value.alerts ? `, ${c.value.alerts} alert(s)` : ''}`,
  })

  const t = status?.token?.value
  if (!t) out.push({ label: 'Dhan token', level: 'idle', value: 'Unknown' })
  else if (t.state === 'not used') out.push({ label: 'Dhan token', level: 'idle', value: 'Not used', detail: 'Demo replays stored data' })
  else if (t.state === 'valid' && t.expires_at) {
    const left = (Date.parse(t.expires_at) / 1000 - now) / 3600
    out.push({ label: 'Dhan token', level: left < 2 ? 'warn' : 'good', value: `Valid, ${left.toFixed(1)} h left`,
      detail: t.auto_refresh ? 'Auto-refresh via TOTP' : 'Pasted token: no auto-refresh' })
  } else out.push({ label: 'Dhan token', level: 'bad', value: t.state === 'error' ? 'Error' : 'Missing', detail: t.error ?? undefined })

  const nv = status?.news?.value
  if (!nv) out.push({ label: 'News feeds', level: 'idle', value: 'Service not seen yet', detail: 'Start `newsalert news`' })
  else if (nv.replay) {
    out.push({ label: 'News (replayed)', level: 'idle',
      value: `${status?.replay?.value?.news_archive?.emitted ?? 0} of ${nv.archived_alerts ?? 0} archived`, detail: nv.note })
  } else {
    const entries = Object.entries(nv.feeds ?? {})
    const bad = entries.filter(([, v]) => v.ok === false)
    const stale = nv.last_poll_at ? now - nv.last_poll_at > 900 : true
    out.push({ label: 'News feeds', level: bad.length === entries.length && entries.length ? 'bad' : bad.length || stale ? 'warn' : 'good',
      value: nv.last_poll_at ? `Polled ${ago(now - nv.last_poll_at)}` : 'Not polled yet',
      detail: [bad.length ? `${bad.length}/${entries.length} feeds failing: ${bad.map(([k]) => k).join(', ')}` : `${entries.length} feeds ok`,
        `${nv.alerts_today ?? 0} alerts today`].join(' · ') })
    const g = nv.gemini
    if (!nv.gemini_configured) out.push({ label: 'Gemini', level: 'warn', value: 'Key not set', detail: 'BusinessLine headlines stay pending' })
    else out.push({
      label: 'Gemini quota', level: g?.paused_until || nv.last_error ? 'warn' : 'good',
      value: g ? `${g.used_today}/${g.daily_cap} requests today` : '—',
      detail: [g?.paused_until ? `Paused until ${fmtTime(g.paused_until, tz)} ${tzAbbr(tz)}` : '',
        nv.pending ? `${nv.pending} headline(s) waiting` : '', nv.last_error ? `Last error: ${nv.last_error}` : ''].filter(Boolean).join(' · ') || undefined,
    })
  }

  out.push({ label: 'Push channel', level: conn === 'open' ? 'good' : 'warn',
    value: conn === 'open' ? 'Connected' : conn === 'connecting' ? 'Connecting…' : 'Reconnecting…' })
  return out
}

export default function StatusBar({ me, status, conn }: { me: Me; status: Status | null; conn: Conn }) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const tz = me.timezone
  const demo = me.mode === 'demo'
  const list = checks(me, status, conn)
  const worst: Level = list.some((c) => c.level === 'bad') ? 'bad' : list.some((c) => c.level === 'warn') ? 'warn' : 'good'
  const problems = list.filter((c) => c.level === 'bad' || c.level === 'warn').length
  const pillText = worst === 'good' ? 'System OK' : worst === 'bad' ? `System: ${problems} problem${problems > 1 ? 's' : ''}`
    : `System: ${problems} warning${problems > 1 ? 's' : ''}`

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => { if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open])

  const m = status?.market
  const marketDetail = !m ? '' : m.open ? (m.now_ts ? `${fmtTime(m.now_ts, tz)} ${tzAbbr(tz)}` : '')
    : m.next_open ? `opens ${fmtTime(Date.parse(m.next_open) / 1000, tz, true)} ${tzAbbr(tz)}` : demo ? 'between replayed sessions' : ''

  return (
    <section className="statusbar2" aria-label="System status">
      <div className="market-state">
        <span className={`dot big ${m?.open ? 'good' : 'idle'}`} aria-hidden="true" />
        <strong>{!m ? 'Market …' : `Market ${m.open ? 'open' : 'closed'}`}</strong>
        {demo && <span className="secondary">(replayed)</span>}
        {marketDetail && <span className="secondary num">· {marketDetail}</span>}
      </div>
      <div className="sys-wrap" ref={wrap}>
        <button type="button" className={`sys-pill sys-${worst}`} aria-expanded={open} aria-controls="sys-pop"
          onClick={() => setOpen((v) => !v)}>
          <span className={`dot ${worst}`} aria-hidden="true" />{pillText}<span aria-hidden="true">{open ? '▴' : '▾'}</span>
        </button>
        {open && (
          <div id="sys-pop" className="sys-pop" role="dialog" aria-label="System details">
            <ul>
              {list.map((c) => (
                <li key={c.label}>
                  <span className={`dot ${c.level}`} aria-hidden="true" />
                  <div>
                    <div className="sys-row"><span className="secondary">{c.label}</span><strong>{c.value}</strong></div>
                    {c.detail && <div className="sys-detail">{c.detail}</div>}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  )
}
