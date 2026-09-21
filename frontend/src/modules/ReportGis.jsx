import { Fragment, useCallback, useEffect, useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState } from '../components/common'
import { useApiList } from '../hooks/useApiList'

const CONFIDENCE_TONE = {
  high: 'var(--green)',
  medium: 'var(--orange)',
  low: 'var(--text-dim)',
}

/**
 * Equirectangular scatter of a GeoJSON FeatureCollection.
 *
 * Still dependency-free. A tiled basemap means an outbound request per tile to
 * whoever hosts the tiles, on every pan, carrying the viewport of whatever an
 * analyst is looking at — which is a bigger decision than "the map would look
 * nicer", and not one to make by adding a dependency.
 *
 * Pin size follows how often a place was mentioned and colour follows
 * confidence, so a grey dot reads as "somewhere in this country" rather than
 * as a location.
 */
function MiniMap({ features, onPick, active }) {
  const project = ([lon, lat]) => ({
    x: ((lon + 180) / 360) * 100,
    y: ((90 - lat) / 180) * 100,
  })

  return (
    <div
      style={{
        position: 'relative',
        aspectRatio: '2 / 1',
        border: '1px solid var(--border-soft)',
        background:
          'repeating-linear-gradient(90deg, rgba(0,240,255,0.07) 0 1px, transparent 1px 8.333%),' +
          'repeating-linear-gradient(rgba(0,240,255,0.07) 0 1px, transparent 1px 16.666%)',
      }}
    >
      {features.map((f) => {
        const { x, y } = project(f.geometry.coordinates)
        const tone = CONFIDENCE_TONE[f.properties.confidence] || 'var(--text-dim)'
        const size = Math.min(16, 7 + (f.properties.mentions || 1))
        const on = active === f.id
        return (
          <button
            key={f.id}
            type="button"
            title={`${f.properties.place_name} — ${f.properties.confidence} confidence`}
            onClick={() => onPick(on ? null : f.id)}
            style={{
              position: 'absolute',
              left: `${x}%`,
              top: `${y}%`,
              width: size,
              height: size,
              margin: `${-size / 2}px 0 0 ${-size / 2}px`,
              padding: 0,
              border: on ? '2px solid var(--text)' : 'none',
              background: tone,
              boxShadow: `0 0 ${on ? 18 : 10}px ${tone}`,
              borderRadius: '50%',
              cursor: 'pointer',
            }}
          />
        )
      })}
      {!features.length && (
        <div
          className="empty"
          style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}
        >
          // No geolocated events
        </div>
      )}
    </div>
  )
}

/** The working behind one pin. */
function ReasonRow({ reason }) {
  const marker = reason.effect === 'limits' ? '~' : '+'
  const tone = reason.effect === 'limits' ? 'var(--yellow)' : 'var(--cyan)'
  return (
    <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 6 }}>
      <span className="mono-dim" style={{ color: tone }}>{marker}</span>
      <span style={{ fontSize: 12, lineHeight: 1.6 }}>{reason.label}</span>
    </div>
  )
}

/** What the geocoder can do, what it refuses to do, and what it cannot. */
function EnginePanel() {
  const [engine, setEngine] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    apiFetch('/api/reportgis/events/engine/').then(setEngine).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="notice bad"><b>Engine unreachable.</b> {error}</div>
  if (!engine) return <div className="empty">// Querying engine…</div>

  const groups = [
    ['Runs', engine.capabilities, 'var(--cyan)', '+'],
    ['Refuses to guess', engine.refuses_to_guess, 'var(--yellow)', '~'],
    ['Not available here', engine.not_available, 'var(--text-dim)', '−'],
  ]

  return (
    <>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
          gap: 20,
        }}
      >
        {groups.map(([title, items, tone, mark]) => (
          <div key={title}>
            <div className="metric-label">{title}</div>
            {(items || []).map((line) => (
              <div key={line} style={{ fontSize: 11, lineHeight: 1.7, color: tone }}>
                {mark} {line}
              </div>
            ))}
          </div>
        ))}
      </div>
      <p className="mono-dim" style={{ fontSize: 11, marginTop: 14, marginBottom: 0 }}>
        Table: {engine.gazetteer.cities} cities, {engine.gazetteer.countries} countries,{' '}
        {engine.gazetteer.regions} regions, of which {engine.gazetteer.ambiguous_names}{' '}
        names belong to more than one place. Engine {engine.engine}.
      </p>
    </>
  )
}

/** Read stored articles for locations. */
function ExtractPanel({ onDone }) {
  const [state, setState] = useState({ phase: 'idle', message: null })
  const busy = state.phase === 'working'

  async function run() {
    setState({ phase: 'working', message: null })
    try {
      const body = await apiFetch('/api/reportgis/events/extract/', {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setState({
        phase: 'done',
        message: `${body.detail} ${body.located} location(s) placed, ${body.not_placed} name(s) found and left unplaced.`,
      })
      onDone()
    } catch (err) {
      setState({ phase: 'error', message: err.message })
    }
  }

  return (
    <>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        <button type="button" className="btn" disabled={busy} onClick={run}>
          {busy ? 'Reading…' : 'Read stored articles'}
        </button>
        <span className="mono-dim" style={{ fontSize: 11 }}>
          Analysed articles this module has not seen yet, 25 at a time.
        </span>
      </div>
      {state.message && (
        <div
          className={`notice ${state.phase === 'error' ? 'bad' : ''}`}
          style={{ marginTop: 12 }}
        >
          {state.message}
        </div>
      )}
    </>
  )
}

export default function ReportGis() {
  const events = useApiList('/api/reportgis/events/')
  const extractions = useApiList('/api/reportgis/extractions/')
  const reports = useApiList('/api/reportgis/reports/')
  const [features, setFeatures] = useState([])
  const [openId, setOpenId] = useState(null)

  const loadMap = useCallback(async () => {
    const body = await apiFetch('/api/reportgis/events/geojson/')
    setFeatures(body.features || [])
  }, [])

  useEffect(() => {
    loadMap().catch(() => setFeatures([]))
  }, [loadMap])

  const reloadAll = () => {
    events.reload()
    extractions.reload()
    loadMap().catch(() => {})
  }

  const counted = (band) => events.rows.filter((e) => e.confidence === band).length
  const unplaced = extractions.rows.reduce((n, run) => n + (run.notes?.length || 0), 0)

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Geo events" tag="MODULE 05">
          <div className="metric">{events.rows.length}</div>
          <div className="metric-label">Locations placed</div>
        </Panel>
        <Panel title="Precisely located" tag="CONFIDENCE">
          <div className="metric" style={{ color: counted('high') ? 'var(--green)' : undefined }}>
            {counted('high')}
          </div>
          <div className="metric-label">Coordinates stated, or name qualified</div>
        </Panel>
        <Panel title="Country-level only" tag="CONFIDENCE">
          <div className="metric" style={{ color: 'var(--text-dim)' }}>{counted('low')}</div>
          <div className="metric-label">A pin inside a country, not a site</div>
        </Panel>
        <Panel title="Found, not placed" tag="REFUSED">
          <div className="metric" style={{ color: unplaced ? 'var(--yellow)' : undefined }}>
            {unplaced}
          </div>
          <div className="metric-label">Names too ambiguous to resolve</div>
        </Panel>
      </div>

      <Panel title="Global event map" tag="EPSG:4326">
        <MiniMap features={features} onPick={setOpenId} active={openId} />
        <div
          style={{
            display: 'flex',
            gap: 18,
            marginTop: 10,
            fontSize: 11,
            color: 'var(--text-dim)',
          }}
        >
          {Object.entries(CONFIDENCE_TONE).map(([band, tone]) => (
            <span key={band} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <i
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  background: tone,
                  display: 'inline-block',
                }}
              />
              {band} confidence
            </span>
          ))}
          <span>· pin size follows how often the place is mentioned</span>
        </div>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Read content for locations" tag="EXTRACT">
        <ExtractPanel onDone={reloadAll} />
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="How a location is decided" tag="SCOPE">
        <EnginePanel />
        <p style={{ fontSize: 12, lineHeight: 1.8, color: 'var(--text-dim)', marginBottom: 0 }}>
          This module reads place names out of text against a{' '}
          <b style={{ color: 'var(--yellow)' }}>table compiled into the source</b> — no
          geocoding service, so no article's place names leave this machine. A name it
          cannot settle is listed as found and not placed rather than resolved to the
          likeliest candidate, because a pin on a map is read as a fact.
        </p>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Placed locations" tag="GEO LOG">
        <DataState {...events} empty="Nothing located yet">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Place</th>
                  <th>Confidence</th>
                  <th>Coordinates</th>
                  <th>How</th>
                  <th>Mentions</th>
                  <th>Article</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {events.rows.map((e) => (
                  <Fragment key={e.id}>
                    <tr>
                      <td>
                        {e.place_name}
                        {e.country && (
                          <span className="mono-dim" style={{ marginLeft: 6 }}>
                            {e.region ? `${e.region}, ` : ''}{e.country}
                          </span>
                        )}
                      </td>
                      <td><Badge value={e.confidence} /></td>
                      <td className="mono-dim">
                        {e.latitude.toFixed(4)}, {e.longitude.toFixed(4)}
                      </td>
                      <td className="mono-dim">{e.method}</td>
                      <td className="mono-dim">{e.mentions}</td>
                      <td className="mono-dim">{e.content_title || '—'}</td>
                      <td>
                        <button
                          type="button"
                          className="btn ghost"
                          onClick={() => setOpenId(openId === e.id ? null : e.id)}
                        >
                          {openId === e.id ? 'Hide' : 'Why'}
                        </button>
                      </td>
                    </tr>

                    {openId === e.id && (
                      <tr>
                        <td colSpan={7} style={{ background: 'rgba(0,0,0,0.35)' }}>
                          <div style={{ padding: '4px 0 12px' }}>
                            <div className="metric-label">How this location was decided</div>
                            <div style={{ marginTop: 6 }}>
                              {e.reasons?.length
                                ? e.reasons.map((r) => <ReasonRow key={r.label} reason={r} />)
                                : <span className="mono-dim">No working recorded.</span>}
                            </div>
                            {e.matched_text && (
                              <div className="mono-dim" style={{ fontSize: 11, marginTop: 10 }}>
                                Read from the text as “{e.matched_text}” · {e.engine_version}
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="What was read, and what was refused" tag="EXTRACTION LOG">
        <DataState {...extractions} empty="No article has been read yet">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Article</th>
                  <th>Placed</th>
                  <th>Left unplaced</th>
                  <th>Engine</th>
                </tr>
              </thead>
              <tbody>
                {extractions.rows.map((run) => (
                  <Fragment key={run.id}>
                    <tr>
                      <td>{run.content_title || `#${run.content_item}`}</td>
                      <td className="mono-dim">{run.events_found}</td>
                      <td
                        className="mono-dim"
                        style={{ color: run.notes?.length ? 'var(--yellow)' : undefined }}
                      >
                        {run.notes?.length || 0}
                      </td>
                      <td className="mono-dim">{run.engine_version}</td>
                    </tr>
                    {run.notes?.length > 0 && (
                      <tr>
                        <td colSpan={4} style={{ background: 'rgba(0,0,0,0.25)' }}>
                          {run.notes.map((note) => (
                            <div
                              key={note}
                              className="mono-dim"
                              style={{ fontSize: 11, padding: '3px 0' }}
                            >
                              ~ {note}
                            </div>
                          ))}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Reports" tag="STATUS">
        <DataState {...reports} empty="No reports generated">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Status</th>
                  <th>Events</th>
                </tr>
              </thead>
              <tbody>
                {reports.rows.map((r) => (
                  <tr key={r.id}>
                    <td>{r.title}</td>
                    <td><Badge value={r.status} /></td>
                    <td className="mono-dim">{r.event_count}</td>
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
