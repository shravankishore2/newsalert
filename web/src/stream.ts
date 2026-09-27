import { useEffect, useRef, useState } from 'react'
import type { Alert, Status } from './api'

export type Conn = 'connecting' | 'open' | 'reconnecting'

/** One EventSource for the whole app: status snapshots + new alerts pushed by the server.
 *  The browser reconnects by itself and resumes with Last-Event-ID, so no alert is lost. */
export function useStream(enabled: boolean, onAlert: (a: Alert) => void, onAuthLost: () => void) {
  const [status, setStatus] = useState<Status | null>(null)
  const [conn, setConn] = useState<Conn>('connecting')
  const alertRef = useRef(onAlert)
  const authRef = useRef(onAuthLost)
  useEffect(() => {
    alertRef.current = onAlert
    authRef.current = onAuthLost
  }, [onAlert, onAuthLost])

  useEffect(() => {
    if (!enabled) return
    const es = new EventSource('/api/stream')
    es.onopen = () => setConn('open')
    es.addEventListener('status', (e) => setStatus(JSON.parse((e as MessageEvent).data)))
    es.addEventListener('alert', (e) => alertRef.current(JSON.parse((e as MessageEvent).data)))
    es.onerror = async () => {
      setConn('reconnecting')
      // EventSource hides the HTTP status; ask the API whether the session is still valid.
      const r = await fetch('/api/me', { credentials: 'same-origin' }).catch(() => null)
      if (r && r.status === 401) {
        es.close()
        authRef.current()
      }
    }
    return () => es.close()
  }, [enabled])

  return { status, conn }
}
