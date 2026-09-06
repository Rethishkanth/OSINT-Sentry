import Panel from '../components/Panel'
import { Badge, DataState, ScoreBar } from '../components/common'
import { useApiList } from '../hooks/useApiList'

export default function Media() {
  const items = useApiList('/api/media/items/')
  const results = useApiList('/api/media/results/')

  const manipulated = results.rows.filter((r) => r.verdict === 'manipulated').length

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Media items" tag="MODULE 04">
          <div className="metric">{items.rows.length}</div>
          <div className="metric-label">Images, video, audio under analysis</div>
        </Panel>
        <Panel title="Forensic runs" tag="ELA / DEEPFAKE">
          <div className="metric">{results.rows.length}</div>
          <div className="metric-label">Tampering assessments</div>
        </Panel>
        <Panel title="Manipulated" tag="VERDICT">
          <div className="metric" style={{ color: manipulated ? 'var(--magenta)' : undefined }}>
            {manipulated}
          </div>
          <div className="metric-label">Confirmed manipulated</div>
        </Panel>
      </div>

      <div className="notice" style={{ marginBottom: 16 }}>
        <b>Detector not installed.</b> The ELA and deepfake models are not present in
        this environment, so no scores are produced. The models, storage, admin, and
        API are wired and ready to receive results.
      </div>

      <Panel title="Media" tag="FORENSIC QUEUE">
        <DataState {...items} empty="No media ingested">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Kind</th>
                  <th>SHA-256</th>
                  <th>Dimensions</th>
                  <th>Verdict</th>
                </tr>
              </thead>
              <tbody>
                {items.rows.map((m) => (
                  <tr key={m.id}>
                    <td>#{m.id}</td>
                    <td className="mono-dim">{m.kind}</td>
                    <td className="mono-dim">
                      {m.sha256 ? `${m.sha256.slice(0, 20)}…` : '—'}
                    </td>
                    <td className="mono-dim">
                      {m.width && m.height ? `${m.width}×${m.height}` : '—'}
                    </td>
                    <td><Badge value={m.latest_verdict} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Tampering results" tag="SCORES">
        <DataState {...results} empty="No forensic runs recorded">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Verdict</th>
                  <th>ELA</th>
                  <th>Deepfake</th>
                  <th>Model</th>
                </tr>
              </thead>
              <tbody>
                {results.rows.map((r) => (
                  <tr key={r.id}>
                    <td><Badge value={r.verdict} /></td>
                    <td className="mono-dim">
                      {r.ela_score === null ? '—' : r.ela_score.toFixed(2)}
                    </td>
                    <td style={{ minWidth: 140 }}>
                      <ScoreBar value={r.deepfake_score} risk />
                    </td>
                    <td className="mono-dim">{r.model_version || '—'}</td>
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
