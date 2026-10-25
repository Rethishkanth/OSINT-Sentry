import { Fragment, useEffect, useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState } from '../components/common'
import { useApiList } from '../hooks/useApiList'

const VERDICT_TONE = {
  authentic: 'var(--green)',
  suspicious: 'var(--orange)',
  manipulated: 'var(--magenta)',
  inconclusive: 'var(--text-dim)',
}

/**
 * One line of the engine's working.
 *
 * A zero-point reason is not filler. It is how this module records something
 * it looked at and decided not to hold against the file - an embedded preview
 * that agreed, or a measurement it takes but deliberately does not score.
 * Dropping those would leave the panel reading as a list of accusations.
 */
function ReasonRow({ reason }) {
  const { label, points } = reason
  const tone = points > 0 ? 'var(--magenta)' : points < 0 ? 'var(--cyan)' : 'var(--text-dim)'

  return (
    <div style={{ display: 'flex', gap: 12, alignItems: 'baseline', marginBottom: 8 }}>
      <span
        className="mono-dim"
        style={{
          minWidth: 46,
          textAlign: 'right',
          color: tone,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {points ? `+${points.toFixed(2)}` : '·'}
      </span>
      <span style={{ fontSize: 12, lineHeight: 1.6 }}>{label}</span>
    </div>
  )
}

/** The observations behind the reasons, for an analyst who wants the raw read. */
function FindingsGrid({ findings }) {
  if (!findings) return null

  const rows = [
    ['Format', findings.format || '-'],
    ['Stored quality', findings.estimated_quality ?? '-'],
    ['Quantization tables', findings.quantization_profile || '-'],
    ['Camera', [findings.camera_make, findings.camera_model].filter(Boolean).join(' ') || '-'],
    ['Software', findings.software || '-'],
    ['Captured', findings.captured_at || '-'],
    ['Modified', findings.modified_at || '-'],
    ['GPS in EXIF', findings.gps_present ? 'present' : 'absent'],
    ['Embedded preview', findings.thumbnail_present
      ? `present (differs by ${(findings.thumbnail_distance ?? 0).toFixed(3)})`
      : 'none'],
    ['Error level map', findings.ela_supported
      ? `largest out-of-step region ${((findings.ela_hotspot_ratio ?? 0) * 100).toFixed(1)}% of frame`
      : 'not applicable'],
  ]

  return (
    <>
      <div className="metric-label" style={{ marginTop: 14 }}>What the file says about itself</div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
          gap: '4px 20px',
          marginTop: 6,
        }}
      >
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: 'flex', gap: 8, fontSize: 11 }}>
            <span className="mono-dim" style={{ minWidth: 130 }}>{label}</span>
            <span>{String(value)}</span>
          </div>
        ))}
      </div>

      {findings.notes?.length > 0 && (
        <>
          <div className="metric-label" style={{ marginTop: 14 }}>Limits of this read</div>
          {findings.notes.map((note) => (
            <div key={note} className="mono-dim" style={{ fontSize: 11, marginTop: 4 }}>
              {note}
            </div>
          ))}
        </>
      )}
    </>
  )
}

/** What the engine can and cannot do, read from the engine itself. */
function EnginePanel() {
  const [engine, setEngine] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    apiFetch('/api/media/items/engine/').then(setEngine).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="notice bad"><b>Engine unreachable.</b> {error}</div>
  if (!engine) return <div className="empty">// Querying engine…</div>

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 20 }}>
      <div>
        <div className="metric-label">Runs</div>
        {engine.capabilities.map((c) => (
          <div key={c} style={{ fontSize: 11, lineHeight: 1.7, color: 'var(--cyan)' }}>+ {c}</div>
        ))}
      </div>
      <div>
        <div className="metric-label">Measured, shown, not scored</div>
        {(engine.reported_not_scored || []).map((c) => (
          <div key={c} style={{ fontSize: 11, lineHeight: 1.7, color: 'var(--yellow)' }}>~ {c}</div>
        ))}
      </div>
      <div>
        <div className="metric-label">Not available here</div>
        {engine.not_available.map((c) => (
          <div key={c} style={{ fontSize: 11, lineHeight: 1.7, color: 'var(--text-dim)' }}>− {c}</div>
        ))}
      </div>
    </div>
  )
}

/** Upload an image and get the read back in the same response. */
function UploadPanel({ onDone }) {
  const [file, setFile] = useState(null)
  const [state, setState] = useState({ phase: 'idle', message: null })
  const busy = state.phase === 'working'

  async function submit(event) {
    event.preventDefault()
    if (!file) return
    setState({ phase: 'working', message: null })
    try {
      const body = new FormData()
      body.append('file', file)
      body.append('kind', 'image')
      const item = await apiFetch('/api/media/items/', { method: 'POST', body })
      setState({
        phase: 'done',
        message: `#${item.id} read: ${item.latest_result?.summary || 'no result recorded'}`,
      })
      setFile(null)
      event.target.reset()
      onDone()
    } catch (err) {
      setState({ phase: 'error', message: err.message })
    }
  }

  return (
    <form onSubmit={submit}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
        <div className="field" style={{ flex: 1, marginBottom: 0 }}>
          <label htmlFor="media-file">Image to examine</label>
          <input
            id="media-file"
            type="file"
            accept="image/*"
            disabled={busy}
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
        </div>
        <button type="submit" className="btn" disabled={busy || !file}>
          {busy ? 'Examining…' : 'Examine'}
        </button>
      </div>

      {state.message && (
        <div
          className={`notice ${state.phase === 'error' ? 'bad' : ''}`}
          style={{ marginTop: 12 }}
        >
          {state.message}
        </div>
      )}

      <p className="mono-dim" style={{ fontSize: 11, marginTop: 12, marginBottom: 0 }}>
        JPEG gives the most to work with: the quantization tables, the embedded
        preview and the EXIF are all JPEG-carried. A PNG can still be hashed and
        read for metadata, and the engine will say so rather than score it.
      </p>
    </form>
  )
}

export default function Media() {
  const items = useApiList('/api/media/items/')
  const [openId, setOpenId] = useState(null)
  const [rerunning, setRerunning] = useState(null)

  const counted = (verdict) =>
    items.rows.filter((m) => m.latest_verdict === verdict).length

  async function rerun(id) {
    setRerunning(id)
    try {
      await apiFetch(`/api/media/items/${id}/analyze/`, { method: 'POST' })
      await items.reload()
    } finally {
      setRerunning(null)
    }
  }

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Media items" tag="MODULE 04">
          <div className="metric">{items.rows.length}</div>
          <div className="metric-label">Files examined</div>
        </Panel>
        <Panel title="Manipulated" tag="VERDICT">
          <div
            className="metric"
            style={{ color: counted('manipulated') ? 'var(--magenta)' : undefined }}
          >
            {counted('manipulated')}
          </div>
          <div className="metric-label">File contradicts its own preview</div>
        </Panel>
        <Panel title="Suspicious" tag="VERDICT">
          <div
            className="metric"
            style={{ color: counted('suspicious') ? 'var(--orange)' : undefined }}
          >
            {counted('suspicious')}
          </div>
          <div className="metric-label">Signs of editing, short of proof</div>
        </Panel>
        <Panel title="Inconclusive" tag="VERDICT">
          <div className="metric" style={{ color: 'var(--text-dim)' }}>
            {counted('inconclusive')}
          </div>
          <div className="metric-label">Not enough to say either way</div>
        </Panel>
      </div>

      <Panel title="Examine an image" tag="UPLOAD">
        <UploadPanel onDone={items.reload} />
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="What this module checks" tag="SCOPE">
        <EnginePanel />
        <p style={{ fontSize: 12, lineHeight: 1.8, color: 'var(--text-dim)', marginBottom: 0 }}>
          This is a <b style={{ color: 'var(--yellow)' }}>provenance and re-encoding
          inspection</b>, not a deepfake detector. It reads what a file records about
          its own history and looks for places that history contradicts itself. A
          synthetic image with clean metadata will pass it, and an honest photograph
          that has been cropped will not.
        </p>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Examined files" tag="FORENSIC LOG">
        <DataState {...items} empty="Nothing examined yet">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Verdict</th>
                  <th>Dimensions</th>
                  <th>SHA-256</th>
                  <th>Engine</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.rows.map((m) => {
                  const result = m.latest_result
                  return (
                    <Fragment key={m.id}>
                      <tr>
                        <td className="mono-dim">#{m.id}</td>
                        <td><Badge value={m.latest_verdict} /></td>
                        <td className="mono-dim">
                          {m.width && m.height ? `${m.width}×${m.height}` : '-'}
                        </td>
                        <td className="mono-dim">
                          {m.sha256 ? `${m.sha256.slice(0, 16)}…` : '-'}
                        </td>
                        <td className="mono-dim">{result?.model_version || '-'}</td>
                        <td style={{ whiteSpace: 'nowrap' }}>
                          <button
                            type="button"
                            className="btn ghost"
                            onClick={() => setOpenId(openId === m.id ? null : m.id)}
                          >
                            {openId === m.id ? 'Hide' : 'Detail'}
                          </button>{' '}
                          <button
                            type="button"
                            className="btn ghost"
                            disabled={rerunning === m.id}
                            onClick={() => rerun(m.id)}
                          >
                            {rerunning === m.id ? '…' : 'Re-run'}
                          </button>
                        </td>
                      </tr>

                      {openId === m.id && (
                        <tr>
                          <td colSpan={6} style={{ background: 'rgba(0,0,0,0.35)' }}>
                            <div style={{ padding: '4px 0 12px' }}>
                              {result ? (
                                <>
                                  <p
                                    style={{
                                      fontSize: 12,
                                      lineHeight: 1.7,
                                      color: VERDICT_TONE[result.verdict],
                                    }}
                                  >
                                    {result.summary}
                                  </p>

                                  <div className="metric-label" style={{ marginTop: 12 }}>
                                    How the verdict was reached
                                  </div>
                                  <div style={{ marginTop: 6 }}>
                                    {result.reasons?.length
                                      ? result.reasons.map((reason) => (
                                          <ReasonRow key={reason.label} reason={reason} />
                                        ))
                                      : <span className="mono-dim">Nothing to report.</span>}
                                  </div>

                                  <FindingsGrid findings={result.details?.findings} />

                                  <div className="mono-dim" style={{ marginTop: 14, fontSize: 11 }}>
                                    {m.sha256 || 'unhashed'} · {result.model_version}
                                  </div>
                                </>
                              ) : (
                                <span className="mono-dim">
                                  No analysis recorded for this file yet.
                                </span>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>
    </div>
  )
}
