import { useCallback, useEffect, useState } from 'react'
import { api, Unauthorized, type Alert, type Me, type NewsAlert } from './api'
import { useRoute } from './route'
import { useStream } from './stream'
import Login from './components/Login'
import ReplayBanner from './components/ReplayBanner'
import StatusBar from './components/StatusBar'
import NewsFeed from './components/NewsFeed'
import NewsDetail from './components/NewsDetail'
import AlertDetail from './components/AlertDetail'
import Performance from './components/Performance'
import Board from './components/Board'
import Link from './components/Link'

type Auth = 'checking' | 'out' | 'in'

export default function App() {
  const [auth, setAuth] = useState<Auth>('checking')
  const [me, setMe] = useState<Me | null>(null)
  const [pushed, setPushed] = useState<Alert[]>([])
  const [pushedNews, setPushedNews] = useState<NewsAlert[]>([])
  const route = useRoute()

  const loadMe = useCallback(async () => {
    try {
      setMe(await api.me())
      setAuth('in')
    } catch (e) {
      if (e instanceof Unauthorized) setAuth('out')
      else throw e
    }
  }, [])

  useEffect(() => { loadMe() }, [loadMe])

  const onAlert = useCallback((a: Alert) => setPushed((p) => [a, ...p].slice(0, 500)), [])
  const onNews = useCallback((n: NewsAlert) => setPushedNews((p) => [n, ...p].slice(0, 500)), [])
  const onAuthLost = useCallback(() => { setAuth('out'); setMe(null) }, [])
  const { status, conn } = useStream(auth === 'in', onAlert, onNews, onAuthLost)

  if (auth === 'checking') return <div className="login"><p className="muted">Loading…</p></div>
  if (auth === 'out' || !me) return <Login onDone={loadMe} />

  const nav = (href: string, label: string, current: boolean) => (
    <Link to={href} aria-current={current ? 'page' : undefined}>{label}</Link>
  )

  return (
    <div className="app">
      {me.mode === 'demo' && <ReplayBanner me={me} status={status} />}
      <header className="topbar">
        <div className="brand"><img src="/favicon.svg" alt="" /> QuantRadar
          {me.mode === 'demo' && <span className="replay-tag" style={{ color: 'var(--replay-ink)', background: 'var(--replay-bg)' }}>REPLAY</span>}
        </div>
        <nav className="nav" aria-label="Main">
          {nav('#/', 'Alerts', route.page === 'feed' || route.page === 'news' || route.page === 'alert')}
          {nav('#/results', 'Results', route.page === 'results')}
          {nav('#/performance', 'Model performance', route.page === 'performance')}
        </nav>
        <div className="spacer" />
        <span className="muted" style={{ fontSize: 13 }}>{me.dataset}</span>
        <button className="btn" onClick={async () => { await api.logout(); onAuthLost() }}>Log out</button>
      </header>
      <StatusBar me={me} status={status} conn={conn} />
      <main className={`main${route.page === 'feed' || route.page === 'results' ? ' wide' : ''}`}>
        {route.page === 'feed' && <NewsFeed me={me} pushedNews={pushedNews} pushedPrice={pushed} conn={conn} />}
        {route.page === 'news' && <NewsDetail key={`n${route.id}`} me={me} id={route.id} />}
        {route.page === 'alert' && <AlertDetail key={route.id} me={me} id={route.id} />}
        {route.page === 'results' && <Board me={me} />}
        {route.page === 'performance' && <Performance />}
      </main>
    </div>
  )
}
