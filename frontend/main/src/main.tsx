import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// First: declares the cascade-layer order every other stylesheet slots into (see the file).
import './styles/tailwind.css'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
