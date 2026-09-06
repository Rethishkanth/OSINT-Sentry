import Panel from '../components/Panel'
import { Badge, DataState } from '../components/common'
import { useApiList } from '../hooks/useApiList'

/**
 * Equirectangular scatter of GeoJSON points. Deliberately dependency-free —
 * Leaflet needs a tile provider, and this module has no data to warrant one
 * until PostGIS is running.
 */
function MiniMap({ features }) {
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
        return (
          <span
            key={f.id}
            title={f.properties?.place_name}
            style={{
              position: 'absolute',
              left: `${x}%`,
              top: `${y}%`,
              width: 8,
              height: 8,
              margin: '-4px 0 0 -4px',
              background: 'var(--magenta)',
              boxShadow: '0 0 10px var(--magenta)',
              borderRadius: '50%',
            }}
          />
        )
      })}
      {!features.length && (
        <div className="empty" style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center' }}>
          // No geolocated events
        </div>
      )}
    </div>
  )
}

export default function ReportGis({ online, reason }) {
  const events = useApiList('/api/reportgis/events/', online)
  const reports = useApiList('/api/reportgis/reports/', online)

  if (!online) {
    return (
      <Panel title="Module 05 — Reporting & GIS" tag="OFFLINE">
        <div className="offline">
          <h2 className="glitch" data-text="MODULE OFFLINE">MODULE OFFLINE</h2>
          <p className="mono-dim" style={{ maxWidth: 480 }}>
            {reason || 'The reportgis app is not loaded.'}
          </p>
          <p className="mono-dim" style={{ maxWidth: 480 }}>
            GeoEvent stores a PostGIS PointField, so the app stays out of
            INSTALLED_APPS until GDAL/GEOS are available. Install OSGeo4W, set
            <b style={{ color: 'var(--yellow)' }}> USE_GIS=True</b> in .env, and
            re-run migrate to bring it online.
          </p>
        </div>
      </Panel>
    )
  }

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Geo events" tag="MODULE 05">
          <div className="metric">{events.rows.length}</div>
          <div className="metric-label">Geolocated incidents</div>
        </Panel>
        <Panel title="Reports" tag="ANALYST OUTPUT">
          <div className="metric">{reports.rows.length}</div>
          <div className="metric-label">Generated dossiers</div>
        </Panel>
      </div>

      <Panel title="Global event map" tag="EPSG:4326">
        <MiniMap features={events.rows} />
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
