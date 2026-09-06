import { useEffect, useRef, useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState, ScoreBar } from '../components/common'
import { useApiList } from '../hooks/useApiList'

// The registry lookup runs on the worker, so a new source appears before its
// result does. Poll the one row rather than showing a score-shaped blank.
const POLL_INTERVAL_MS = 1500
const POLL_ATTEMPTS = 8

/** Registration age, in the largest unit that stays readable. */
function DomainAge({ days }) {
  if (days === null || days === undefined) {
    return <span className="mono-dim" title="Registry lookup has not resolved">—</span>
  }
  const label = days >= 365 ? `${Math.floor(days / 365)}y` : `${days}d`
  // Under six months is the band the scorer penalises; make it visible.
  const fresh = days < 180
  return (
    <span
      className="mono-dim"
      style={{ color: fresh ? 'var(--magenta)' : 'var(--text)' }}
      title={`Registered ${days} days ago`}
    >
      {label}
      {fresh && ' ⚠'}
    </span>
  )
}

/** Submit a website — a pasted article URL or a bare domain — for scoring. */
function AddSource({ onResolved }) {
  const [value, setValue] = useState('')
  const [state, setState] = useState({ phase: 'idle', message: null })
  // Kept in a ref so an unmount mid-poll cannot set state on a dead component.
  // Set on mount as well as cleared on unmount: StrictMode runs effects
  // mount -> cleanup -> mount, so clearing alone leaves it false forever and
  // every poll silently bails.
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])

  /** Wait for the worker to write back a registry result for this source. */
  async function awaitLookup(id, domain) {
    for (let i = 0; i < POLL_ATTEMPTS; i += 1) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS))
      if (!live.current) return
      let row
      try {
        row = await apiFetch(`/api/sources/${id}/`)
      } catch {
        continue // a transient read failure should not end the wait
      }
      if (!live.current) return
      if (row.registry_status) {
        onResolved()
        setState({
          phase: 'done',
          message: row.domain_age_days === null
            ? `${domain}: registry returned “${row.registry_status}” — scored with age unknown.`
            : `${domain}: registered ${Math.floor(row.domain_age_days / 365)} years ago. Scored.`,
        })
        return
      }
    }
    // Not an error — the row exists, the answer just has not landed yet.
    onResolved()
    setState({
      phase: 'done',
      message: `${domain} added. The registry lookup is still running — `
        + 'reload in a moment, or check the Celery worker is up.',
    })
  }

  async function submit(event) {
    event.preventDefault()
    const raw = value.trim()
    if (!raw) return

    setState({ phase: 'working', message: 'Submitting…' })
    try {
      const result = await apiFetch('/api/sources/', {
        method: 'POST',
        body: JSON.stringify({ domain: raw }),
      })
      setValue('')
      onResolved()
      setState({ phase: 'working', message: result.detail })
      await awaitLookup(result.id, result.domain)
    } catch (err) {
      if (live.current) setState({ phase: 'error', message: err.message })
    }
  }

  const busy = state.phase === 'working'

  return (
    <Panel title="Check a website" tag="ADD SOURCE">
      <form onSubmit={submit}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label htmlFor="add-source">Website address</label>
            <input
              id="add-source"
              type="text"
              value={value}
              disabled={busy}
              onChange={(e) => setValue(e.target.value)}
              placeholder="https://www.bbc.co.uk/news/article-123   or just   bbc.co.uk"
              autoComplete="off"
              spellCheck="false"
            />
          </div>
          <button type="submit" className="btn" disabled={busy || !value.trim()}>
            {busy ? 'Checking…' : 'Check'}
          </button>
        </div>
      </form>

      <div className="mono-dim" style={{ marginTop: 10 }}>
        Paste a full link or a bare domain — it is reduced to the registrable
        domain, so a BBC article and news.bbc.co.uk both resolve to bbc.co.uk.
      </div>

      {state.message && (
        <div
          className={`notice ${state.phase === 'error' ? 'bad' : ''}`}
          style={{ marginTop: 12 }}
        >
          {state.message}
        </div>
      )}
    </Panel>
  )
}

export default function Credibility() {
  const scores = useApiList('/api/credibility/?ordering=-created_at')
  const sources = useApiList('/api/sources/')
  const [busy, setBusy] = useState(null)
  const [note, setNote] = useState(null)

  const resolved = sources.rows.filter((s) => s.domain_age_days !== null)
  const unresolved = sources.rows.filter((s) => s.domain_age_days === null)

  async function rescore(sourceId) {
    setBusy(sourceId)
    setNote(null)
    try {
      await apiFetch('/api/credibility/score/', {
        method: 'POST',
        body: JSON.stringify({ source: sourceId, wait: true }),
      })
      // Scoring also refreshes the cached registry data, so the sources table
      // (domain age, registrar) is stale too — reload both.
      await Promise.all([scores.reload(), sources.reload()])
      setNote('Scoring complete.')
    } catch (err) {
      setNote(err.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Tracked sources" tag="MODULE 01">
          <div className="metric">{sources.rows.length}</div>
          <div className="metric-label">Registered origins</div>
        </Panel>
        <Panel title="Assessments" tag="TOTAL">
          <div className="metric">{scores.rows.length}</div>
          <div className="metric-label">Credibility records</div>
        </Panel>
        <Panel title="High trust" tag="TIER">
          <div className="metric">
            {scores.rows.filter((s) => ['high', 'verified'].includes(s.trust_tier)).length}
          </div>
          <div className="metric-label">Scored high or verified</div>
        </Panel>
        <Panel title="Registry coverage" tag="RDAP">
          <div className="metric" style={{ color: resolved.length ? 'var(--cyan)' : 'var(--text-dim)' }}>
            {resolved.length}
            <span style={{ fontSize: 18, color: 'var(--text-dim)' }}>
              /{sources.rows.length}
            </span>
          </div>
          <div className="metric-label">Domains with a real registration date</div>
          {unresolved.length > 0 && (
            <div className="mono-dim" style={{ marginTop: 12 }}>
              {unresolved.length} unresolved — scored with age unknown, not as new
            </div>
          )}
        </Panel>
      </div>

      <AddSource
        onResolved={() => { scores.reload(); sources.reload() }}
      />

      <div style={{ height: 16 }} />

      {note && <div className="notice" style={{ marginBottom: 16 }}>{note}</div>}

      <div className="notice" style={{ marginBottom: 16 }}>
        <b>Registration metadata only.</b> Scores come from RDAP registry data —
        domain age, registrar, TLD — which separates a domain created last week
        from one created in 1994. It carries no information about whether the
        content is accurate, so metadata alone is capped below the VERIFIED tier.
      </div>

      <Panel title="Sources" tag="TRIGGER SCORING">
        <DataState {...sources} empty="No sources registered">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Domain</th>
                  <th>Publisher</th>
                  <th>Domain age</th>
                  <th>Registry</th>
                  <th>Latest score</th>
                  <th>Active</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {sources.rows.map((s) => (
                  <tr key={s.id}>
                    <td>{s.domain}</td>
                    <td className="mono-dim">{s.publisher || '—'}</td>
                    <td><DomainAge days={s.domain_age_days} /></td>
                    <td title={s.registrar || 'Registrar unknown'}>
                      <Badge value={s.registry_status} />
                    </td>
                    <td style={{ minWidth: 140 }}>
                      <ScoreBar value={s.latest_credibility_score} max={100} />
                    </td>
                    {/* Not a tier — reusing 'verified' here read as a trust
                        claim next to the real tier column. */}
                    <td><Badge value={s.is_active ? 'active' : 'inactive'} /></td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost"
                        disabled={busy === s.id}
                        onClick={() => rescore(s.id)}
                      >
                        {busy === s.id ? 'Scoring…' : 'Rescore'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Assessment log" tag="PROVENANCE TRACKED">
        <DataState {...scores} empty="No assessments yet">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Score</th>
                  <th>Tier</th>
                  <th>Model</th>
                  <th>Rationale</th>
                </tr>
              </thead>
              <tbody>
                {scores.rows.map((s) => (
                  <tr key={s.id}>
                    <td>{s.source_domain}</td>
                    <td style={{ minWidth: 140 }}>
                      <ScoreBar value={s.credibility_score} max={100} />
                    </td>
                    <td><Badge value={s.trust_tier} /></td>
                    <td className="mono-dim">{s.model_version}</td>
                    <td className="mono-dim" style={{ maxWidth: 380 }}>{s.rationale}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>
    </div>
  )
}
