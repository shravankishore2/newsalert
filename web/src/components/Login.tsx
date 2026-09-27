import { useState, type FormEvent } from 'react'
import { api } from '../api'

export default function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    const r = await api.login(pw)
    setBusy(false)
    if (r.ok) onDone()
    else setError(r.error)
  }

  return (
    <div className="login">
      <form onSubmit={submit}>
        <h1>Market Alerts</h1>
        <p>Private dashboard. The news sources' licences allow personal use only, so it needs a password.</p>
        <label className="sr-only" htmlFor="pw">Password</label>
        <input id="pw" type="password" autoComplete="current-password" autoFocus placeholder="Password"
          value={pw} onChange={(e) => setPw(e.target.value)} />
        {error && <div className="error" role="alert">{error}</div>}
        <button className="btn primary" disabled={busy || !pw}>{busy ? 'Checking…' : 'Log in'}</button>
      </form>
    </div>
  )
}
