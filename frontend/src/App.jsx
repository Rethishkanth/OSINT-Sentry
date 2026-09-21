import { useCallback, useEffect, useState } from 'react'

import { apiFetch, clearTokens, hasToken } from './api'
import Cursor from './components/Cursor'
import Login from './components/Login'
import Content from './modules/Content'
import Credibility from './modules/Credibility'
import Media from './modules/Media'
import Overview from './modules/Overview'
import ReportGis from './modules/ReportGis'
import Social from './modules/Social'
import './theme.css'

const TABS = [
  { key: 'overview', idx: '00', label: 'Overview' },
  { key: 'credibility', idx: '01', label: 'Credibility' },
  { key: 'content', idx: '02', label: 'Content' },
  { key: 'social', idx: '03', label: 'Social' },
  { key: 'media', idx: '04', label: 'Media' },
  { key: 'reportgis', idx: '05', label: 'Report / GIS' },
]

function Clock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(id)
  }, [])
  return <span>{now.toISOString().slice(11, 19)} UTC</span>
}

export default function App() {
  const [authed, setAuthed] = useState(hasToken)
  const [tab, setTab] = useState('overview')
  const [overview, setOverview] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const loadOverview = useCallback(async () => {
    if (!authed) return
    setLoading(true)
    setError(null)
    try {
      setOverview(await apiFetch('/api/overview/'))
    } catch (err) {
      // A dead token should drop us back to the login screen, not spin.
      if (err.status === 401) {
        clearTokens()
        setAuthed(false)
      }
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [authed])

  useEffect(() => {
    loadOverview()
  }, [loadOverview])

  function signOut() {
    clearTokens()
    setAuthed(false)
    setOverview(null)
    setTab('overview')
  }

  if (!authed) {
    return (
      <>
        <div className="ambience" />
        <div className="glow-follow" />
        <Cursor />
        <Login onAuthed={() => setAuthed(true)} />
      </>
    )
  }

  // Module 5 used to drive an "GIS offline" light here, because it genuinely
  // could be. It no longer can, so the light counts modules instead of
  // singling one out.
  const modules = Object.values(overview?.modules ?? {})
  const onlineCount = modules.filter((m) => m.online).length

  return (
    <>
      <div className="ambience" />
      <div className="glow-follow" />
      <Cursor />

      <div className="shell">
        <header className="topbar">
          <div className="brand">
            <span className="glitch" data-text="OSINT">OSINT</span>
            <span>·</span>SENTRY
          </div>
          <div className="status-line">
            <span><i className="dot" />Uplink active</span>
            <span>
              <i className={`dot ${onlineCount === modules.length ? '' : 'warn'}`} />
              {onlineCount}/{modules.length} modules
            </span>
            <Clock />
            <button type="button" className="btn ghost" onClick={signOut}>
              Disconnect
            </button>
          </div>
        </header>

        <nav className="nav" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={tab === t.key}
              onClick={() => setTab(t.key)}
            >
              <span className="idx">{t.idx}</span>
              {t.label}
            </button>
          ))}
        </nav>

        <main>
          {tab === 'overview' && (
            <Overview
              data={overview}
              loading={loading}
              error={error}
              onSelect={setTab}
            />
          )}
          {tab === 'credibility' && <Credibility />}
          {tab === 'content' && <Content />}
          {tab === 'social' && <Social />}
          {tab === 'media' && <Media />}
          {tab === 'reportgis' && <ReportGis />}
        </main>
      </div>
    </>
  )
}
