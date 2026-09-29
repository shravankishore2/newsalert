import { useState } from 'react'

type Theme = 'light' | 'dark'
const KEY = 'qr-theme'

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'))
  const next: Theme = theme === 'dark' ? 'light' : 'dark'
  const toggle = () => {
    document.documentElement.dataset.theme = next
    try { localStorage.setItem(KEY, next) } catch { /* ignore */ }
    setTheme(next)
  }
  return (
    <button type="button" className="btn theme-toggle" onClick={toggle} aria-label={`Switch to ${next} mode`} title={`Switch to ${next} mode`}>
      <span aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span> {next === 'dark' ? 'Dark' : 'Light'}
    </button>
  )
}
