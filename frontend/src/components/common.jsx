export function Badge({ value }) {
  if (value === null || value === undefined || value === '') {
    return <span className="mono-dim">—</span>
  }
  return <span className={`badge ${String(value).toLowerCase()}`}>{value}</span>
}

/** 0–1 score as a bar. `risk` flips the colour ramp so high means bad. */
export function ScoreBar({ value, max = 1, risk = false }) {
  if (value === null || value === undefined) return <span className="mono-dim">—</span>
  const pct = Math.max(0, Math.min(100, (value / max) * 100))
  const tone = risk ? (pct >= 70 ? 'risk' : pct >= 40 ? 'warn' : '') : ''
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span className={`bar ${tone}`} style={{ flex: 1 }}>
        <i style={{ width: `${pct}%` }} />
      </span>
      <span className="mono-dim" style={{ minWidth: 34, textAlign: 'right' }}>
        {Number(value).toFixed(2)}
      </span>
    </span>
  )
}

/**
 * A rating out of 10 with its tag. `score` may be null, which is a real
 * state and not an error: both engines withhold a number rather than guess
 * when they could not actually check.
 */
export function TenPointRating({ score, label, tone = 'var(--text)', size = 22 }) {
  const has = score !== null && score !== undefined
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <span style={{ fontSize: size, fontWeight: 700, color: tone, lineHeight: 1 }}>
        {has ? score.toFixed(1) : '—'}
        {has && <span style={{ fontSize: size * 0.5, color: 'var(--text-dim)' }}>/10</span>}
      </span>
      <span className="badge" style={{ color: tone, borderColor: tone }}>{label}</span>
    </span>
  )
}

export function Empty({ children = 'No records' }) {
  return <div className="empty">// {children}</div>
}

export function Loading() {
  return <div className="empty">// Querying…</div>
}

export function ErrorNote({ error }) {
  return (
    <div className="notice bad">
      <b>Query failed.</b> {error}
    </div>
  )
}

/** Wraps a table view in its loading / error / empty states. */
export function DataState({ loading, error, rows, children, empty }) {
  if (loading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!rows.length) return <Empty>{empty}</Empty>
  return children
}
