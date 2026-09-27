import { useEffect, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, type ResultSection } from '../api'

export default function Results() {
  const [sections, setSections] = useState<ResultSection[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { api.results().then((r) => setSections(r.sections)).catch((e) => setError(String(e))) }, [])

  if (error) return <div className="empty error">{error}</div>
  if (!sections) return <p className="muted">Loading…</p>
  if (!sections.length) return <div className="empty">docs/RESULTS.md has no results sections yet.</div>

  return (
    <>
      <div className="feed-head"><h1>Replay results</h1><span className="muted">from docs/RESULTS.md</span></div>
      {sections.map((s) => (
        <section className="results-section" key={s.key} aria-labelledby={`r-${s.key}`}>
          <h2 id={`r-${s.key}`}>{s.title}</h2>
          {s.has_numbers ? (
            <>
              <div className="tiles">
                {s.false_alert_rows.map((r) => (
                  <div className="tile" key={r.variant}>
                    <div className="label">False-alert rate · {r.variant.toLowerCase()}</div>
                    <div className="big num">{r.rate.toFixed(1)}%</div>
                    <div className="sub num">95% CI {r.ci_low.toFixed(1)}–{r.ci_high.toFixed(1)}% · {r.false}/{r.n} evaluable · {r.alerts} alerts</div>
                  </div>
                ))}
              </div>
              {s.date_range && <p className="muted" style={{ marginTop: 0 }}>{s.date_range}</p>}
            </>
          ) : (
            <div className="notice">No numbers for this market yet: the replay hasn't been run. Details below.</div>
          )}
          <details open={!s.has_numbers}>
            <summary className="secondary" style={{ cursor: 'pointer', marginBottom: 8 }}>Full section</summary>
            <div className="markdown"><Markdown remarkPlugins={[remarkGfm]}>{s.markdown.replace(/^## .+\n/, '')}</Markdown></div>
          </details>
        </section>
      ))}
    </>
  )
}
