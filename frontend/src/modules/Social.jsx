import { useMemo, useRef, useState } from 'react'

import Panel from '../components/Panel'
import { DataState, ScoreBar } from '../components/common'
import { useApiList } from '../hooks/useApiList'

/**
 * Force-free graph render: accounts on a circle, edges as chords.
 * Nodes light up as the pointer approaches — proximity is computed in SVG
 * user units so it stays correct at any viewport size.
 */
function GraphMap({ accounts, edges }) {
  const svgRef = useRef(null)
  const [near, setNear] = useState(null)

  const positions = useMemo(() => {
    const map = new Map()
    accounts.forEach((a, i) => {
      const angle = (i / Math.max(1, accounts.length)) * Math.PI * 2 - Math.PI / 2
      map.set(a.id, {
        x: 200 + Math.cos(angle) * 140,
        y: 200 + Math.sin(angle) * 140,
        account: a,
      })
    })
    return map
  }, [accounts])

  function onMove(event) {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    // Map client pixels into the 400x400 viewBox.
    const x = ((event.clientX - rect.left) / rect.width) * 400
    const y = ((event.clientY - rect.top) / rect.height) * 400

    let closest = null
    let best = 46
    positions.forEach((pos, id) => {
      const dist = Math.hypot(pos.x - x, pos.y - y)
      if (dist < best) {
        best = dist
        closest = id
      }
    })
    setNear(closest)
  }

  if (!accounts.length) return <div className="empty">// No accounts in graph</div>

  return (
    <div>
      <svg
        ref={svgRef}
        viewBox="0 0 400 400"
        style={{ width: '100%', height: 'auto', display: 'block' }}
        onMouseMove={onMove}
        onMouseLeave={() => setNear(null)}
      >
        {edges.map((e) => {
          const from = positions.get(e.from_account)
          const to = positions.get(e.to_account)
          if (!from || !to) return null
          const active = near === e.from_account || near === e.to_account
          return (
            <line
              key={e.id}
              x1={from.x} y1={from.y} x2={to.x} y2={to.y}
              stroke={active ? 'var(--yellow)' : 'rgba(0,240,255,0.22)'}
              strokeWidth={active ? 1.6 : 0.7}
            />
          )
        })}
        {[...positions.values()].map(({ x, y, account }) => {
          const risk = account.bot_likelihood ?? 0
          const active = near === account.id
          return (
            <g key={account.id}>
              <circle
                cx={x} cy={y} r={active ? 9 : 5.5}
                fill={risk >= 0.7 ? 'var(--magenta)' : risk >= 0.4 ? 'var(--orange)' : 'var(--green)'}
                opacity={active ? 1 : 0.85}
              />
              {active && (
                <text
                  x={x} y={y - 15}
                  textAnchor="middle"
                  fill="var(--yellow)"
                  style={{ fontSize: 11, fontFamily: 'var(--mono)' }}
                >
                  {account.handle}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      <div className="mono-dim" style={{ textAlign: 'center', marginTop: 6 }}>
        Hover a node to trace its edges · red = high bot likelihood
      </div>
    </div>
  )
}

export default function Social() {
  const accounts = useApiList('/api/social/accounts/?ordering=-bot_likelihood')
  const edges = useApiList('/api/social/edges/')
  const posts = useApiList('/api/social/posts/')

  const flagged = accounts.rows.filter((a) => (a.bot_likelihood ?? 0) >= 0.7)

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Accounts" tag="MODULE 03">
          <div className="metric">{accounts.rows.length}</div>
          <div className="metric-label">Tracked identities</div>
          <div className="sub-metrics">
            <div><b>{edges.rows.length}</b>Edges</div>
            <div><b>{posts.rows.length}</b>Posts</div>
          </div>
        </Panel>
        <Panel title="Flagged" tag="BOT ≥ 0.70">
          <div className="metric" style={{ color: flagged.length ? 'var(--magenta)' : undefined }}>
            {flagged.length}
          </div>
          <div className="metric-label">Suspected automation</div>
        </Panel>
        <Panel title="Interaction graph" tag="LIVE">
          <GraphMap accounts={accounts.rows} edges={edges.rows} />
        </Panel>
      </div>

      <Panel title="Accounts" tag="COORDINATION ANALYSIS">
        <DataState {...accounts} empty="No accounts tracked">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Handle</th>
                  <th>Platform</th>
                  <th>Bot likelihood</th>
                  <th>Coordination</th>
                  <th>Model</th>
                </tr>
              </thead>
              <tbody>
                {accounts.rows.map((a) => (
                  <tr key={a.id}>
                    <td>@{a.handle}</td>
                    <td className="mono-dim">{a.platform}</td>
                    <td style={{ minWidth: 150 }}>
                      <ScoreBar value={a.bot_likelihood} risk />
                    </td>
                    <td style={{ minWidth: 150 }}>
                      <ScoreBar value={a.coordination_score} risk />
                    </td>
                    <td className="mono-dim">{a.graph_model_version || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Posts" tag="SENTIMENT / TOXICITY">
        <DataState {...posts} empty="No posts captured">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Account</th>
                  <th>Text</th>
                  <th>Sentiment</th>
                  <th>Toxicity</th>
                </tr>
              </thead>
              <tbody>
                {posts.rows.map((p) => (
                  <tr key={p.id}>
                    <td>@{p.handle}</td>
                    <td className="mono-dim" style={{ maxWidth: 420 }}>{p.text}</td>
                    <td className="mono-dim">
                      {p.sentiment === null ? '—' : p.sentiment.toFixed(2)}
                    </td>
                    <td style={{ minWidth: 140 }}>
                      <ScoreBar value={p.toxicity} risk />
                    </td>
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
