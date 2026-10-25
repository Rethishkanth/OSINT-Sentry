import Panel from '../components/Panel'

const MODULES = [
  { key: 'credibility', idx: '01', name: 'Credibility engine', primary: 'scores', label: 'Assessments' },
  { key: 'content', idx: '02', name: 'Content analysis', primary: 'items', label: 'Content items' },
  { key: 'social', idx: '03', name: 'Social graph', primary: 'accounts', label: 'Accounts' },
  { key: 'media', idx: '04', name: 'Media forensics', primary: 'items', label: 'Media items' },
  { key: 'reportgis', idx: '05', name: 'Reporting & GIS', primary: 'events', label: 'Geo events' },
]

export default function Overview({ data, loading, error, onSelect }) {
  if (loading) return <div className="empty">// Establishing uplink…</div>
  if (error) return <div className="notice bad"><b>Uplink failed.</b> {error}</div>
  if (!data) return null

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        {MODULES.map((m) => {
          const stats = data.modules[m.key] || {}
          const online = stats.online
          return (
            <Panel
              key={m.key}
              title={`${m.idx} · ${m.name}`}
              tag={online ? 'ONLINE' : 'OFFLINE'}
              className={online ? '' : 'is-offline'}
            >
              <button
                type="button"
                onClick={() => onSelect(m.key)}
                style={{
                  all: 'unset',
                  cursor: 'pointer',
                  display: 'block',
                  width: '100%',
                }}
              >
                <div
                  className="metric"
                  style={{ color: online ? undefined : 'var(--text-dim)' }}
                >
                  {online ? (stats[m.primary] ?? 0) : '-'}
                </div>
                <div className="metric-label">{m.label}</div>

                {online ? (
                  <div className="sub-metrics">
                    {Object.entries(stats)
                      .filter(([k]) => !['online', 'reason', m.primary, 'model_version'].includes(k))
                      .slice(0, 3)
                      .map(([k, v]) => (
                        <div key={k}>
                          <b>{v}</b>
                          {k}
                        </div>
                      ))}
                  </div>
                ) : (
                  <div className="mono-dim" style={{ marginTop: 12 }}>{stats.reason}</div>
                )}

                {stats.model_version && (
                  <div className="mono-dim" style={{ marginTop: 12 }}>
                    engine: {stats.model_version}
                  </div>
                )}
              </button>
            </Panel>
          )
        })}

        <Panel title="Audit trail" tag="CHAIN OF CUSTODY">
          <div className="metric" style={{ color: 'var(--cyan)' }}>{data.audit_entries}</div>
          <div className="metric-label">Recorded write events</div>
        </Panel>
      </div>

      <Panel title="Engine notice" tag="READ THIS">
        <p style={{ fontSize: 12, lineHeight: 1.8, color: 'var(--text-dim)', margin: 0 }}>
          Scores shown across this console come from <b style={{ color: 'var(--yellow)' }}>heuristic
          placeholder engines</b>, not trained models. Every record carries the engine
          version that produced it, so placeholder output is never mistaken for a
          model verdict. Swapping in a real model is a settings change.
        </p>
      </Panel>
    </div>
  )
}
