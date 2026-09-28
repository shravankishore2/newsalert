/** Speedometer gauge: five bands from strong negative to strong positive, a needle, and the
 *  outcome in words (never colour alone). value === null draws no needle. */
type Props = { value: number | null; min: number; max: number; label: string; caption: string; size?: number }

const BANDS = [
  { from: 0, to: 0.2, cls: 'g-neg2' }, { from: 0.2, to: 0.4, cls: 'g-neg1' }, { from: 0.4, to: 0.6, cls: 'g-mid' },
  { from: 0.6, to: 0.8, cls: 'g-pos1' }, { from: 0.8, to: 1, cls: 'g-pos2' },
]

function polar(cx: number, cy: number, r: number, frac: number) {
  const a = Math.PI * (1 - frac)          // 0 -> left (180deg), 1 -> right (0deg)
  return [cx + r * Math.cos(a), cy - r * Math.sin(a)]
}

function arc(cx: number, cy: number, r: number, f0: number, f1: number) {
  const [x0, y0] = polar(cx, cy, r, f0)
  const [x1, y1] = polar(cx, cy, r, f1)
  return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 0 1 ${x1.toFixed(2)},${y1.toFixed(2)}`
}

export default function Gauge({ value, min, max, label, caption, size = 180 }: Props) {
  const w = size, h = size * 0.62, cx = w / 2, cy = h - 8, r = w / 2 - 14
  const frac = value === null ? null : Math.max(0, Math.min(1, (value - min) / (max - min)))
  const needle = frac === null ? null : polar(cx, cy, r - 12, frac)
  return (
    <figure className="gauge" style={{ width: w }}>
      <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`${caption}: ${label}`}>
        {BANDS.map((b) => (
          <path key={b.cls} d={arc(cx, cy, r, b.from + 0.006, b.to - 0.006)} className={`g-band ${b.cls}`} />
        ))}
        {needle && (
          <>
            <line x1={cx} y1={cy} x2={needle[0]} y2={needle[1]} className="g-needle" />
            <circle cx={cx} cy={cy} r={5} className="g-hub" />
          </>
        )}
        {!needle && <text x={cx} y={cy - 4} textAnchor="middle" className="g-na">no data yet</text>}
      </svg>
      <figcaption>
        <strong className="g-label">{label}</strong>
        <span className="g-caption">{caption}</span>
      </figcaption>
    </figure>
  )
}
