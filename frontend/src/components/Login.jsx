import { useState } from 'react'

import { login } from '../api'
import Panel from './Panel'

export default function Login({ onAuthed }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, password)
      onAuthed()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <Panel title="Authentication required" tag="JWT">
          <div className="brand" style={{ fontSize: 20, marginBottom: 6 }}>
            <span className="glitch" data-text="OSINT">OSINT</span>
            <span>·</span>SENTRY
          </div>
          <p className="login-lede">
            Analyst console. Credentials are exchanged for a bearer token at
            /api/auth/token/.
          </p>

          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="u">Operator</label>
              <input
                id="u"
                type="text"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="p">Passphrase</label>
              <input
                id="p"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>

            {error && (
              <div className="notice bad" style={{ marginBottom: 14 }}>{error}</div>
            )}

            <button type="submit" className="btn" disabled={busy} style={{ width: '100%' }}>
              {busy ? 'Authenticating…' : 'Establish link'}
            </button>
          </form>

          <p className="mono-dim" style={{ marginTop: 18, marginBottom: 0, fontSize: 11 }}>
            No account? Create one with
            {' '}<code style={{ color: 'var(--cyan)' }}>manage.py createsuperuser</code>.
          </p>
        </Panel>
      </div>
    </div>
  )
}
