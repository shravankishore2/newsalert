import { useEffect, useMemo, useRef, useState } from 'react'
import { fmtTime, money, pct, tzAbbr } from '../api'

type Pt = [number, number]
type Props = {
  symbol: string; indexName: string; prices: Pt[]; index: Pt[]
  refTs: number; alertTs: number; start: number; end: number; tz: string; currency: string
  markers?: { ts: number; label: string }[]   // default: Reference + Alert
  baseLabel?: string                           // what the % change is measured from
}

// The SVG is drawn at the container's real pixel width so text stays readable on phones
// (a fixed viewBox would scale 11px labels down to ~5px on a 360px screen).
function useWidth(ref: React.RefObject<HTMLDivElement | null>) {
  const [w, setW] = useState(760)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setW(Math.max(300, Math.round(e.contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return w
}

/** Both series rebased to % change since the reference price, so a stock and an index
 *  with very different price levels share one honest axis (no dual axis). */
function rebase(pts: Pt[], refTs: number): [number, number, number][] {
  if (!pts.length) return []
  let base = pts[0][1]
  for (const [t, v] of pts) { if (t <= refTs) base = v; else break }
  return pts.map(([t, v]) => [t, v / base - 1, v])
}

function niceTicks(lo: number, hi: number, n = 5) {
  const span = hi - lo || 0.01
  const raw = span / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) out.push(Number(v.toFixed(10)))
  return out
}

function nearest(series: [number, number, number][], t: number) {
  if (!series.length) return null
  let best = series[0]
  for (const p of series) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p
  return best
}

export default function PriceChart(p: Props) {
  const [hoverT, setHoverT] = useState<number | null>(null)
  const [table, setTable] = useState(false)
  const svgRef = useRef<SVGSVGElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const W = useWidth(wrapRef)
  const narrow = W < 560
  const H = narrow ? 230 : 280
  const M = { top: 16, right: narrow ? 92 : 118, bottom: 30, left: narrow ? 44 : 52 }
  const s = useMemo(() => rebase(p.prices, p.refTs), [p.prices, p.refTs])
  const ix = useMemo(() => rebase(p.index, p.refTs), [p.index, p.refTs])

  if (s.length < 2) return <div ref={wrapRef}><p className="muted">Not enough price data around this alert to draw a chart.</p></div>

  // axis spans the data actually present (e.g. stops at the close, not an empty after-hours stretch)
  const x0 = Math.min(s[0][0], ix[0]?.[0] ?? s[0][0])
  const x1 = Math.max(s[s.length - 1][0], ix[ix.length - 1]?.[0] ?? 0)
  const ys = [...s, ...ix].map((d) => d[1]).concat(0)
  let lo = Math.min(...ys), hi = Math.max(...ys)
  const pad = (hi - lo) * 0.08 || 0.005
  lo -= pad; hi += pad
  const iw = W - M.left - M.right, ih = H - M.top - M.bottom
  const X = (t: number) => M.left + ((t - x0) / (x1 - x0 || 1)) * iw
  const Y = (v: number) => M.top + (1 - (v - lo) / (hi - lo)) * ih
  const path = (d: [number, number, number][]) => d.map(([t, v], i) => `${i ? 'L' : 'M'}${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join('')
  const yTicks = niceTicks(lo, hi)
  const step = Math.max(900, Math.round((x1 - x0) / (narrow ? 4 : 6) / 900) * 900)
  const xt = [] as number[]
  for (let t = Math.ceil(x0 / step) * step; t <= x1; t += step) xt.push(t)

  // direct labels at line ends, nudged apart if they would collide
  const lastS = s[s.length - 1], lastI = ix[ix.length - 1]
  let yS = Y(lastS[1]), yI = lastI ? Y(lastI[1]) : 0
  if (lastI && Math.abs(yS - yI) < 16) { const mid = (yS + yI) / 2; const up = yS <= yI; yS = mid + (up ? -8 : 8); yI = mid + (up ? 8 : -8) }

  function onMove(e: React.PointerEvent) {
    const r = svgRef.current!.getBoundingClientRect()
    const px = ((e.clientX - r.left) / r.width) * W
    if (px < M.left || px > W - M.right) { setHoverT(null); return }
    setHoverT(x0 + ((px - M.left) / iw) * (x1 - x0))
  }
  const hs = hoverT !== null ? nearest(s, hoverT) : null
  const hi_ = hoverT !== null ? nearest(ix, hoverT) : null

  return (
    <div className="chart">
      <div className="chart-legend">
        <span><span className="swatch" style={{ background: 'var(--series-1)' }} />{p.symbol}</span>
        <span><span className="swatch" style={{ background: 'var(--series-2)' }} />{p.indexName}</span>
        <span className="muted">% change since {p.baseLabel ?? 'the reference price'} ({fmtTime(p.refTs, p.tz)} {tzAbbr(p.tz)})</span>
        <span className="spacer" />
        <button className="linkish" onClick={() => setTable((v) => !v)}>{table ? 'Hide data table' : 'Show data table'}</button>
      </div>
      <div className="chart-wrap" ref={wrapRef}>
        <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} role="img"
          aria-label={`${p.symbol} and ${p.indexName}, percent change since the reference time`}
          onPointerMove={onMove} onPointerLeave={() => setHoverT(null)}>
          {yTicks.map((v) => (
            <g key={v}>
              <line className={v === 0 ? 'zero' : 'gridline'} x1={M.left} x2={W - M.right} y1={Y(v)} y2={Y(v)} />
              <text className="axis-label" x={M.left - 8} y={Y(v) + 4} textAnchor="end">{pct(v, 1)}</text>
            </g>
          ))}
          {xt.map((t) => (
            <text key={t} className="axis-label" x={X(t)} y={H - 8} textAnchor="middle">{fmtTime(t, p.tz)}</text>
          ))}
          {/* reference label sits left of its line, alert label right: they can't collide */}
          {(p.markers ? p.markers.map((m) => [m.ts, m.label, 'start', 4] as const)
            : ([[p.refTs, 'Reference', 'end', -4], [p.alertTs, 'Alert', 'start', 4]] as const)).map(([t, label, anchor, dx]) => (
            <g key={label}>
              <line className="marker" x1={X(t)} x2={X(t)} y1={M.top} y2={H - M.bottom} />
              <text className="marker-label" x={X(t) + dx} y={M.top + 10} textAnchor={anchor}>{label}</text>
            </g>
          ))}
          {ix.length > 1 && <path className="series" d={path(ix)} style={{ stroke: 'var(--series-2)' }} />}
          <path className="series" d={path(s)} style={{ stroke: 'var(--series-1)' }} />
          <text className="end-label" x={X(lastS[0]) + 8} y={yS + 4}>{p.symbol} {pct(lastS[1], 1)}</text>
          {lastI && <text className="end-label" x={X(lastI[0]) + 8} y={yI + 4}>{p.indexName} {pct(lastI[1], 1)}</text>}
          {hs && (
            <g>
              <line className="crosshair" x1={X(hs[0])} x2={X(hs[0])} y1={M.top} y2={H - M.bottom} />
              {hi_ && <circle className="hover-dot" cx={X(hi_[0])} cy={Y(hi_[1])} r={4.5} fill="var(--series-2)" />}
              <circle className="hover-dot" cx={X(hs[0])} cy={Y(hs[1])} r={4.5} fill="var(--series-1)" />
            </g>
          )}
        </svg>
        {hs && (
          <div className="tooltip" style={{
            left: `${(X(hs[0]) / W) * 100}%`, top: 8,
            transform: X(hs[0]) > W * 0.6 ? 'translateX(calc(-100% - 12px))' : 'translateX(12px)',
          }}>
            <div className="t">{fmtTime(hs[0], p.tz, true)} {tzAbbr(p.tz)}</div>
            <div><span className="swatch" style={{ background: 'var(--series-1)' }} />{p.symbol} <strong className="num">{pct(hs[1])}</strong> <span className="muted num">{money(hs[2], p.currency)}</span></div>
            {hi_ && <div><span className="swatch" style={{ background: 'var(--series-2)' }} />{p.indexName} <strong className="num">{pct(hi_[1])}</strong> <span className="muted num">{hi_[2].toLocaleString('en-IN', { maximumFractionDigits: 2 })}</span></div>}
          </div>
        )}
      </div>
      {table && (
        <div className="table-scroll">
          <table className="data">
            <thead><tr><th>Time ({tzAbbr(p.tz)})</th><th>{p.symbol}</th><th>{p.symbol} %</th><th>{p.indexName}</th><th>{p.indexName} %</th></tr></thead>
            <tbody className="num">
              {s.map(([t, v, raw]) => {
                const i = nearest(ix, t)
                return <tr key={t}><td>{fmtTime(t, p.tz)}</td><td>{money(raw, p.currency)}</td><td>{pct(v)}</td>
                  <td>{i ? i[2].toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—'}</td><td>{i ? pct(i[1]) : '—'}</td></tr>
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
