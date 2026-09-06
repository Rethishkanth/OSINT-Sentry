import { useState } from 'react'

import Panel from '../components/Panel'
import { Badge, DataState } from '../components/common'
import { useApiList } from '../hooks/useApiList'

export default function Content() {
  const items = useApiList('/api/content/?ordering=-created_at')
  const [selectedId, setSelectedId] = useState(null)

  const selected = items.rows.find((i) => i.id === selectedId) || items.rows[0] || null

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Ingested" tag="MODULE 02">
          <div className="metric">{items.rows.length}</div>
          <div className="metric-label">Items past the credibility gate</div>
        </Panel>
        <Panel title="Analyzed" tag="NLP">
          <div className="metric">
            {items.rows.filter((i) => i.status === 'analyzed').length}
          </div>
          <div className="metric-label">Entities and topics extracted</div>
        </Panel>
        <Panel title="Entities" tag="EXTRACTED">
          <div className="metric">
            {items.rows.reduce((sum, i) => sum + (i.entities?.length || 0), 0)}
          </div>
          <div className="metric-label">Named entity mentions</div>
        </Panel>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.2fr) minmax(0, 1fr)', gap: 16 }}>
        <Panel title="Content stream" tag="SELECT TO INSPECT">
          <DataState {...items} empty="Nothing ingested">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Source</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {items.rows.map((i) => (
                    <tr
                      key={i.id}
                      onClick={() => setSelectedId(i.id)}
                      style={{
                        cursor: 'pointer',
                        background: selected?.id === i.id ? 'rgba(252,238,10,0.07)' : undefined,
                      }}
                    >
                      <td>{i.title || <span className="mono-dim">{i.url}</span>}</td>
                      <td className="mono-dim">{i.source_domain}</td>
                      <td><Badge value={i.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataState>
        </Panel>

        <Panel title="Analysis" tag={selected ? `ITEM #${selected.id}` : 'NO SELECTION'}>
          {!selected ? (
            <div className="empty">// Select an item</div>
          ) : (
            <div>
              <div className="metric-label">Summary</div>
              <p style={{ fontSize: 12, lineHeight: 1.7, color: 'var(--text)' }}>
                {selected.summary || <span className="mono-dim">Not analyzed</span>}
              </p>

              <div className="metric-label" style={{ marginTop: 18 }}>Entities</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
                {selected.entities?.length ? (
                  selected.entities.map((e) => (
                    <span key={e.id} className="badge" style={{ color: 'var(--cyan)' }}>
                      {e.text} · {e.label} ×{e.mention_count}
                    </span>
                  ))
                ) : (
                  <span className="mono-dim">None found</span>
                )}
              </div>

              <div className="metric-label" style={{ marginTop: 18 }}>Topics</div>
              <div style={{ marginTop: 8 }}>
                {selected.topics?.length ? (
                  selected.topics.map((t) => (
                    <div
                      key={t.id}
                      style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}
                    >
                      <span style={{ minWidth: 110, fontSize: 12 }}>{t.label}</span>
                      <span className="bar" style={{ flex: 1 }}>
                        <i style={{ width: `${Math.min(100, t.weight * 400)}%` }} />
                      </span>
                      <span className="mono-dim">{t.weight.toFixed(3)}</span>
                    </div>
                  ))
                ) : (
                  <span className="mono-dim">None found</span>
                )}
              </div>

              <div className="mono-dim" style={{ marginTop: 18 }}>
                Analyzer: {selected.analyzer_version || '—'}
              </div>
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}
