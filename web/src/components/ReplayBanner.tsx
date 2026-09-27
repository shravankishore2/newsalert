import { fmtTime, tzAbbr, type Me, type Status } from '../api'

/** Always visible in demo mode: nothing on screen is live. */
export default function ReplayBanner({ me, status }: { me: Me; status: Status | null }) {
  const r = status?.replay?.value
  const frac = r && r.total_minutes ? r.minutes_done / r.total_minutes : 0
  return (
    <div className="replay-banner" role="note" aria-label="Replay data notice">
      <span className="replay-tag">REPLAY DATA</span>
      <span>
        <strong>Not live.</strong> Historical prices from {me.dataset}, replayed through the real alert logic
        {r ? ` at ${r.speed}× speed` : ''}. Times shown are replayed market times.
      </span>
      {r?.sim_ts && (
        <span className="num">Replay clock: <strong>{fmtTime(r.sim_ts, me.timezone, true)} {tzAbbr(me.timezone)}</strong></span>
      )}
      {r && (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <span className="progress" role="progressbar" aria-valuenow={Math.round(frac * 100)} aria-valuemin={0}
            aria-valuemax={100} aria-label="Replay progress"><span style={{ width: `${frac * 100}%` }} /></span>
          <span className="num">{r.finished ? 'finished' : `${Math.round(frac * 100)}%`}</span>
        </span>
      )}
    </div>
  )
}
