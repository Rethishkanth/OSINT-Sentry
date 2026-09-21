import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { apiDownload, apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState } from '../components/common'
import { useApiList } from '../hooks/useApiList'
import { LAND_PATH } from './worldOutline'

const CONFIDENCE_TONE = {
  high: 'var(--green)',
  medium: 'var(--orange)',
  low: 'var(--text-dim)',
}

const CONFIDENCE_RANK = { low: 1, medium: 2, high: 3 }

// The map's user space: x = lon + 180, y = 90 - lat. The whole world is
// 360 x 180, so a viewBox *is* a lat/lon window and nothing gets projected
// twice.
const WORLD = { x: 0, y: 0, w: 360 }
const MIN_SPAN = 4 // degrees of longitude; below this the outline is noise
const PIN_UNITS = 2.6 // pin radius in user units at full zoom

const project = ([lon, lat]) => [lon + 180, 90 - lat]

function clampView({ x, y, w }) {
  const span = Math.min(WORLD.w, Math.max(MIN_SPAN, w))
  const height = span / 2
  return {
    w: span,
    x: Math.min(Math.max(x, 0), WORLD.w - span),
    y: Math.min(Math.max(y, 0), 180 - height),
  }
}

/** The tightest window holding every pin, with a margin. */
function fitTo(features) {
  if (!features.length) return clampView({ ...WORLD })
  const points = features.map((f) => project(f.geometry.coordinates))
  const xs = points.map((p) => p[0])
  const ys = points.map((p) => p[1])
  const pad = 8
  const left = Math.min(...xs) - pad
  const right = Math.max(...xs) + pad
  const top = Math.min(...ys) - pad
  const bottom = Math.max(...ys) + pad
  const w = Math.max(right - left, (bottom - top) * 2)
  return clampView({ x: left - (w - (right - left)) / 2, y: top, w })
}

/**
 * Pins landing on the same spot at the current zoom, collapsed into one.
 *
 * Without this, a city named in nine articles is nine circles stacked exactly
 * on top of each other, which reads as one — so the map would show less the
 * more there was to show.
 */
function cluster(features, view) {
  const cell = view.w / 55
  const buckets = new Map()
  for (const feature of features) {
    const [x, y] = project(feature.geometry.coordinates)
    const key = `${Math.round(x / cell)}:${Math.round(y / cell)}`
    const bucket = buckets.get(key)
    if (bucket) bucket.members.push(feature)
    else buckets.set(key, { key, x, y, members: [feature] })
  }
  return [...buckets.values()].map((bucket) => ({
    ...bucket,
    // The strongest pin in the group decides the colour: a cluster holding
    // one precisely located event must not be greyed out by the
    // country-level pins sitting on top of it.
    lead: bucket.members.reduce((a, b) =>
      (CONFIDENCE_RANK[b.properties.confidence] || 0) >
      (CONFIDENCE_RANK[a.properties.confidence] || 0)
        ? b
        : a,
    ),
    mentions: bucket.members.reduce((n, f) => n + (f.properties.mentions || 1), 0),
  }))
}

/**
 * The event map: a compiled-in coastline, a graticule, and the pins.
 *
 * Equirectangular, which is wrong about area and right about coordinates —
 * the axes read the same as the numbers in the database. Nothing is measured
 * off the screen; every distance this module reports comes from a haversine
 * over the stored latitude and longitude.
 */
function EventMap({ features, onPick, active }) {
  const [view, setView] = useState(() => clampView({ ...WORLD }))
  const [hover, setHover] = useState(null)
  const [panning, setPanning] = useState(false)
  const surface = useRef(null)
  const drag = useRef(null)

  const height = view.w / 2
  const scale = view.w / WORLD.w
  const groups = useMemo(() => cluster(features, view), [features, view])
  const step = view.w <= 30 ? 5 : view.w <= 120 ? 10 : 30
  const labelled = view.w <= 90

  const zoom = useCallback((factor) => {
    setView((current) => {
      const centre = { x: current.x + current.w / 2, y: current.y + current.w / 4 }
      const next = Math.min(WORLD.w, Math.max(MIN_SPAN, current.w * factor))
      const ratio = next / current.w
      return clampView({
        w: next,
        x: centre.x - (centre.x - current.x) * ratio,
        y: centre.y - (centre.y - current.y) * ratio,
      })
    })
  }, [])

  // Registered by hand rather than as an onWheel prop: React listens for
  // wheel passively, so preventDefault from a prop handler is ignored and the
  // page scrolls away underneath the zoom. A wheel over a map means zoom, and
  // only zoom.
  useEffect(() => {
    const node = surface.current
    if (!node) return undefined
    const onWheel = (event) => {
      event.preventDefault()
      zoom(event.deltaY > 0 ? 1.25 : 0.8)
    }
    node.addEventListener('wheel', onWheel, { passive: false })
    return () => node.removeEventListener('wheel', onWheel)
  }, [zoom])

  const onPointerDown = (event) => {
    drag.current = { px: event.clientX, py: event.clientY, view, moved: false }
    setPanning(true)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  const onPointerMove = (event) => {
    if (!drag.current || !surface.current) return
    const rect = surface.current.getBoundingClientRect()
    const perPixel = drag.current.view.w / rect.width
    const dx = (event.clientX - drag.current.px) * perPixel
    const dy = (event.clientY - drag.current.py) * perPixel
    if (Math.abs(dx) + Math.abs(dy) > 0.4) drag.current.moved = true
    setView(
      clampView({
        ...drag.current.view,
        x: drag.current.view.x - dx,
        y: drag.current.view.y - dy,
      }),
    )
  }

  const endPan = () => {
    // Kept for one tick past pointerup so the click handler can tell a pan
    // from a selection; React clears it on the next render.
    setPanning(false)
    window.setTimeout(() => {
      drag.current = null
    }, 0)
  }

  const pick = (group) => {
    if (drag.current?.moved) return // that click ended a pan
    const id = group.lead.id
    onPick(active === id ? null : id)
  }

  const gridlines = []
  for (let x = Math.ceil(view.x / step) * step; x <= view.x + view.w; x += step) {
    gridlines.push(
      <line key={`v${x}`} x1={x} y1={view.y} x2={x} y2={view.y + height} className="grat" />,
    )
  }
  for (let y = Math.ceil(view.y / step) * step; y <= view.y + height; y += step) {
    gridlines.push(
      <line key={`h${y}`} x1={view.x} y1={y} x2={view.x + view.w} y2={y} className="grat" />,
    )
  }

  const caption = hover || features.find((f) => f.id === active) || null

  return (
    <div>
      <style>{`
        .grat { stroke: rgba(0,240,255,0.10); stroke-width: 0.5px;
                vector-effect: non-scaling-stroke; }
        .land { fill: rgba(0,240,255,0.055); stroke: rgba(0,240,255,0.32);
                stroke-width: 1px; vector-effect: non-scaling-stroke; }
        .pin  { cursor: pointer; }
      `}</style>

      <div
        ref={surface}
        style={{
          position: 'relative',
          border: '1px solid var(--border-soft)',
          background: 'rgba(0,0,0,0.30)',
          touchAction: 'none',
        }}
      >
        <svg
          viewBox={`${view.x} ${view.y} ${view.w} ${height}`}
          style={{ display: 'block', width: '100%', cursor: panning ? 'grabbing' : 'grab' }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endPan}
          onPointerLeave={endPan}
        >
          {gridlines}
          <path d={LAND_PATH} className="land" />

          {groups.map((group) => {
            const tone = CONFIDENCE_TONE[group.lead.properties.confidence] || 'var(--text-dim)'
            const many = group.members.length > 1
            const on = group.members.some((f) => f.id === active)
            const r = (PIN_UNITS + Math.min(2.2, group.mentions * 0.25)) * scale
            return (
              <g
                key={group.key}
                className="pin"
                onClick={() => pick(group)}
                onMouseEnter={() => setHover(group.lead)}
                onMouseLeave={() => setHover(null)}
              >
                <title>
                  {group.lead.properties.place_name}
                  {many ? ` and ${group.members.length - 1} more here` : ''}
                </title>
                <circle
                  cx={group.x}
                  cy={group.y}
                  r={r}
                  fill={tone}
                  fillOpacity={many ? 0.55 : 0.85}
                  stroke={on ? 'var(--text)' : tone}
                  strokeWidth={on ? 2 : 1}
                  vectorEffect="non-scaling-stroke"
                />
                {many && (
                  <text
                    x={group.x}
                    y={group.y + r * 0.42}
                    textAnchor="middle"
                    style={{ fontSize: r * 1.2, fill: '#05080c', fontWeight: 700 }}
                  >
                    {group.members.length}
                  </text>
                )}
                {labelled && !many && (
                  <text
                    x={group.x + r * 1.5}
                    y={group.y + r * 0.5}
                    style={{ fontSize: 3.4 * scale, fill: 'var(--text-dim)', pointerEvents: 'none' }}
                  >
                    {group.lead.properties.place_name}
                  </text>
                )}
              </g>
            )
          })}
        </svg>

        <div style={{ position: 'absolute', top: 8, right: 8, display: 'flex', gap: 6 }}>
          <button type="button" className="btn ghost" onClick={() => zoom(0.6)}>+</button>
          <button type="button" className="btn ghost" onClick={() => zoom(1.7)}>−</button>
          <button type="button" className="btn ghost" onClick={() => setView(fitTo(features))}>
            Fit
          </button>
          <button
            type="button"
            className="btn ghost"
            onClick={() => setView(clampView({ ...WORLD }))}
          >
            World
          </button>
        </div>

        {!features.length && (
          <div
            className="empty"
            style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}
          >
            // No geolocated events
          </div>
        )}
      </div>

      <div
        className="mono-dim"
        style={{
          fontSize: 11,
          padding: '7px 2px',
          minHeight: 20,
          borderBottom: '1px solid var(--border-soft)',
        }}
      >
        {caption ? (
          <>
            <b style={{ color: CONFIDENCE_TONE[caption.properties.confidence] }}>
              {caption.properties.place_name}
            </b>
            {caption.properties.country ? ` · ${caption.properties.country}` : ''}
            {' · '}
            {caption.geometry.coordinates[1].toFixed(4)},{' '}
            {caption.geometry.coordinates[0].toFixed(4)} · {caption.properties.confidence}{' '}
            confidence · mentioned {caption.properties.mentions}×
          </>
        ) : (
          'Drag to pan, scroll to zoom, click a pin to read its working below.'
        )}
      </div>
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

const FIELD = {
  background: 'rgba(0,0,0,0.4)',
  border: '1px solid var(--border-soft)',
  color: 'var(--text)',
  font: 'inherit',
  fontSize: 12,
  padding: '6px 8px',
  width: '100%',
}

/**
 * Choose a set of events, then write a report about them.
 *
 * Counting before writing is deliberate. The backend refuses to produce a
 * report over an empty selection — a document headed with a scope and
 * containing nothing reads as "we looked and nothing happened" — and finding
 * that out from a red error after pressing the button is a worse way to learn
 * it than seeing the count first.
 */
function ReportBuilder({ selected, onGenerated }) {
  const [scope, setScope] = useState({ confidence: '', country: '', since: '', until: '' })
  const [radius, setRadius] = useState('')
  const [title, setTitle] = useState('')
  const [preview, setPreview] = useState(null)
  const [state, setState] = useState({ phase: 'idle', message: null })

  const payload = () => {
    const body = {}
    for (const [key, value] of Object.entries(scope)) {
      if (value) body[key] = value
    }
    if (selected && radius) {
      body.near = `${selected.latitude},${selected.longitude}`
      body.within_km = Number(radius)
    }
    return body
  }

  const set = (key) => (event) => {
    setScope({ ...scope, [key]: event.target.value })
    setPreview(null)
  }

  async function check() {
    setState({ phase: 'working', message: null })
    try {
      setPreview(
        await apiFetch('/api/reportgis/reports/preview/', {
          method: 'POST',
          body: JSON.stringify({ scope: payload() }),
        }),
      )
      setState({ phase: 'idle', message: null })
    } catch (err) {
      setPreview(null)
      setState({ phase: 'error', message: err.message })
    }
  }

  async function generate() {
    setState({ phase: 'working', message: null })
    try {
      const report = await apiFetch('/api/reportgis/reports/generate/', {
        method: 'POST',
        body: JSON.stringify({ scope: payload(), title: title || undefined }),
      })
      setState({ phase: 'done', message: `Written: ${report.title}` })
      onGenerated(report)
    } catch (err) {
      setState({ phase: 'error', message: err.message })
    }
  }

  const busy = state.phase === 'working'

  return (
    <>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
          gap: 12,
          marginBottom: 14,
        }}
      >
        <label>
          <div className="metric-label">Confidence floor</div>
          <select style={FIELD} value={scope.confidence} onChange={set('confidence')}>
            <option value="">Any</option>
            <option value="medium">Medium and better</option>
            <option value="high">High only</option>
          </select>
        </label>
        <label>
          <div className="metric-label">Country</div>
          <input
            style={FIELD}
            placeholder="Libya, or LY"
            value={scope.country}
            onChange={set('country')}
          />
        </label>
        <label>
          <div className="metric-label">Reported from</div>
          <input style={FIELD} type="date" value={scope.since} onChange={set('since')} />
        </label>
        <label>
          <div className="metric-label">Reported until</div>
          <input style={FIELD} type="date" value={scope.until} onChange={set('until')} />
        </label>
        <label>
          <div className="metric-label">
            {selected ? `Km around ${selected.place_name}` : 'Km around a pin'}
          </div>
          <input
            style={{ ...FIELD, opacity: selected ? 1 : 0.5 }}
            type="number"
            min="1"
            placeholder={selected ? '250' : 'select a pin first'}
            disabled={!selected}
            value={radius}
            onChange={(event) => {
              setRadius(event.target.value)
              setPreview(null)
            }}
          />
        </label>
        <label>
          <div className="metric-label">Title (optional)</div>
          <input
            style={FIELD}
            placeholder="written for you if left blank"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
      </div>

      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        <button type="button" className="btn ghost" disabled={busy} onClick={check}>
          Count what this covers
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy || preview?.events === 0}
          onClick={generate}
        >
          {busy ? 'Writing…' : 'Write the report'}
        </button>
        {preview && (
          <span className="mono-dim" style={{ fontSize: 11 }}>
            {preview.detail}
            {preview.events > 0 && (
              <>
                {' · '}
                {preview.countries} country/ies · {preview.articles} article(s) ·{' '}
                {preview.by_confidence.high} high, {preview.by_confidence.medium} medium,{' '}
                {preview.by_confidence.low} low
              </>
            )}
          </span>
        )}
      </div>

      {state.message && (
        <div className={`notice ${state.phase === 'error' ? 'bad' : ''}`} style={{ marginTop: 12 }}>
          {state.message}
        </div>
      )}
    </>
  )
}

/** One report, opened: its caveats first, then the document as written. */
function ReportView({ report, onRegenerate, busy }) {
  return (
    <div style={{ padding: '4px 0 12px' }}>
      <div className="metric-label">What this report does not show</div>
      <div style={{ margin: '6px 0 16px' }}>
        {(report.gaps || []).map((gap) => (
          <div
            key={gap}
            style={{
              display: 'flex',
              gap: 10,
              alignItems: 'baseline',
              marginBottom: 6,
              fontSize: 12,
              lineHeight: 1.6,
            }}
          >
            <span className="mono-dim" style={{ color: 'var(--yellow)' }}>~</span>
            <span>{gap}</span>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn ghost"
          onClick={() =>
            apiDownload(`/api/reportgis/reports/${report.id}/download/`, 'report.md')
          }
        >
          Download .md
        </button>
        <button type="button" className="btn ghost" disabled={busy} onClick={onRegenerate}>
          {busy ? 'Rebuilding…' : 'Rebuild from the same scope'}
        </button>
        <span className="mono-dim" style={{ fontSize: 11, alignSelf: 'center' }}>
          {report.builder_version} · {report.event_count} event(s) ·{' '}
          {report.generated_at
            ? new Date(report.generated_at).toLocaleString()
            : 'never generated'}
        </span>
      </div>

      <pre
        style={{
          background: 'rgba(0,0,0,0.45)',
          border: '1px solid var(--border-soft)',
          padding: 14,
          fontSize: 11,
          lineHeight: 1.7,
          maxHeight: 460,
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
          margin: 0,
        }}
      >
        {report.document}
      </pre>
    </div>
  )
}

export default function ReportGis() {
  const events = useApiList('/api/reportgis/events/')
  const extractions = useApiList('/api/reportgis/extractions/')
  const reports = useApiList('/api/reportgis/reports/')
  const [features, setFeatures] = useState([])
  const [totals, setTotals] = useState(null)
  const [openId, setOpenId] = useState(null)
  const [openReport, setOpenReport] = useState(null)
  const [rebuilding, setRebuilding] = useState(false)

  // The headline figures come from the server, not from the rows on screen.
  // The lists below are paginated, so counting them here would report the
  // first twenty-five of everything as if it were everything.
  const loadMap = useCallback(async () => {
    const [geojson, summary] = await Promise.all([
      apiFetch('/api/reportgis/events/geojson/'),
      apiFetch('/api/reportgis/events/summary/'),
    ])
    setFeatures(geojson.features || [])
    setTotals(summary)
  }, [])

  useEffect(() => {
    loadMap().catch(() => setFeatures([]))
  }, [loadMap])

  const reloadAll = () => {
    events.reload()
    extractions.reload()
    loadMap().catch(() => {})
  }

  const counted = (band) => totals?.by_confidence?.[band] ?? 0
  const unplaced = totals?.not_placed ?? 0
  const selected = events.rows.find((e) => e.id === openId) || null

  async function openReportById(id) {
    if (openReport?.id === id) {
      setOpenReport(null)
      return
    }
    setOpenReport(await apiFetch(`/api/reportgis/reports/${id}/`))
  }

  async function regenerate(id) {
    setRebuilding(true)
    try {
      setOpenReport(
        await apiFetch(`/api/reportgis/reports/${id}/regenerate/`, { method: 'POST' }),
      )
      reports.reload()
    } finally {
      setRebuilding(false)
    }
  }

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Geo events" tag="MODULE 05">
          <div className="metric">{totals?.events ?? '—'}</div>
          <div className="metric-label">
            Locations placed{totals ? ` · ${totals.countries} countries` : ''}
          </div>
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
        <EventMap features={features} onPick={setOpenId} active={openId} />
        <div
          style={{
            display: 'flex',
            gap: 18,
            marginTop: 10,
            fontSize: 11,
            color: 'var(--text-dim)',
            flexWrap: 'wrap',
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
          <span>· a numbered pin is that many events on one spot</span>
        </div>
        <p className="mono-dim" style={{ fontSize: 11, marginTop: 10, marginBottom: 0 }}>
          The coastline is an outline compiled into this console — schematic, no borders, no
          tiles, and <b style={{ color: 'var(--yellow)' }}>nothing fetched from anywhere</b>,
          so no viewport leaves the machine. Distances are computed from the stored
          coordinates, never from the screen.
        </p>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Write a report" tag="REPORTING">
        <ReportBuilder
          selected={selected}
          onGenerated={(report) => {
            reports.reload()
            setOpenReport(report)
          }}
        />
        <p style={{ fontSize: 12, lineHeight: 1.8, color: 'var(--text-dim)', margin: '14px 0 0' }}>
          A report is Markdown assembled from counted figures — nothing here writes prose
          about what the events <i>mean</i>. Its longest section is{' '}
          <b style={{ color: 'var(--yellow)' }}>what the report cannot show</b>: the pins that
          are country-level only, the names found and refused, the articles nobody has
          cross-referenced. A selection that matches nothing produces no report at all,
          because an empty one reads as a finding that nothing happened.
        </p>
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
                  <th>Caveats</th>
                  <th>Generated</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {reports.rows.map((r) => (
                  <Fragment key={r.id}>
                    <tr>
                      <td>{r.title}</td>
                      <td><Badge value={r.status} /></td>
                      <td className="mono-dim">{r.event_count}</td>
                      <td className="mono-dim" style={{ color: 'var(--yellow)' }}>
                        {r.gap_count}
                      </td>
                      <td className="mono-dim">
                        {r.generated_at ? new Date(r.generated_at).toLocaleDateString() : '—'}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="btn ghost"
                          onClick={() => openReportById(r.id)}
                        >
                          {openReport?.id === r.id ? 'Close' : 'Read'}
                        </button>
                      </td>
                    </tr>
                    {openReport?.id === r.id && (
                      <tr>
                        <td colSpan={6} style={{ background: 'rgba(0,0,0,0.35)' }}>
                          <ReportView
                            report={openReport}
                            busy={rebuilding}
                            onRegenerate={() => regenerate(r.id)}
                          />
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
    </div>
  )
}
