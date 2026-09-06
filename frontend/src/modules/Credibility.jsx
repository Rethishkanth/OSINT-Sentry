import { useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState, ScoreBar } from '../components/common'
import { useApiList } from '../hooks/useApiList'

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
