import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'
import GuestApp from './components/GuestApp'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {location.pathname === '/guest' ? <GuestApp /> : <App />}
  </StrictMode>,
)
