import { useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState, ScoreBar } from '../components/common'
import { useApiList } from '../hooks/useApiList'

export default function Credibility() {
  const scores = useApiList('/api/credibility/?ordering=-created_at')
  const sources = useApiList('/api/sources/')
  const [busy, setBusy] = useState(null)
  const [note, setNote] = useState(null)

  async function rescore(sourceId) {
    setBusy(sourceId)
    setNote(null)
    try {
      await apiFetch('/api/credibility/score/', {
        method: 'POST',
        body: JSON.stringify({ source: sourceId, wait: true }),
      })
      await scores.reload()
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
      </div>

      {note && <div className="notice" style={{ marginBottom: 16 }}>{note}</div>}

      <Panel title="Sources" tag="TRIGGER SCORING">
        <DataState {...sources} empty="No sources registered">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Domain</th>
                  <th>Publisher</th>
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
                    <td style={{ minWidth: 140 }}>
                      <ScoreBar value={s.latest_credibility_score} max={100} />
                    </td>
                    <td><Badge value={s.is_active ? 'verified' : 'low'} /></td>
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
