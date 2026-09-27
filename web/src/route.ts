import { useEffect, useState } from 'react'

export type Route = { page: 'feed' } | { page: 'alert'; id: number } | { page: 'news'; id: number } | { page: 'results' }

function parse(hash: string): Route {
  const m = hash.match(/^#\/alerts\/(\d+)/)
  if (m) return { page: 'alert', id: Number(m[1]) }
  const n = hash.match(/^#\/news\/(\d+)/)
  if (n) return { page: 'news', id: Number(n[1]) }
  if (hash.startsWith('#/results')) return { page: 'results' }
  return { page: 'feed' }
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(location.hash))
  useEffect(() => {
    const on = () => setRoute(parse(location.hash))
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, [])
  return route
}
