import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { DataState, ScoreBar } from '../components/common'
import { useApiList } from '../hooks/useApiList'

const HIGH = 0.7
const MIDDLING = 0.4

/**
 * The colour of a 0-1 risk score, with its own answer for "not scored".
 *
 * Grey rather than green: both scorers withhold a number when the graph or
 * the lexicon will not carry one, and an unjudged account painted the same
 * colour as a cleared one is the whole failure this module is trying to
 * avoid.
 */
function riskTone(value) {
  if (value === null || value === undefined) return 'var(--text-dim)'
  if (value >= HIGH) return 'var(--magenta)'
  if (value >= MIDDLING) return 'var(--orange)'
  return 'var(--green)'
}

/** A score cell that says *why* it is empty rather than showing a dash. */
function ScoreCell({ value }) {
  if (value === null || value === undefined) {
    return <span className="mono-dim">not scored</span>
  }
  return <ScoreBar value={value} risk />
}

/**
 * One line of a scorer's working.
 *
 * A 'limits' line is the point of the panel, not a footnote on it: it is how
 * the module records something it looked at and refused to turn into a
 * number. Those lines are marked and kept at full weight.
 */
function ReasonRow({ reason }) {
  const limits = reason.effect === 'limits'
  return (
    <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', marginBottom: 10 }}>
      <span className="mono-dim" style={{ color: limits ? 'var(--yellow)' : 'var(--cyan)' }}>
        {limits ? '~' : '+'}
      </span>
      <span>
        <span style={{ fontSize: 12, lineHeight: 1.6 }}>{reason.label}</span>
        {reason.detail && (
          <div
            className="mono-dim"
            style={{ fontSize: 11, lineHeight: 1.6, marginTop: 2, maxWidth: 680 }}
          >
            {reason.detail}
          </div>
        )}
      </span>
    </div>
  )
}

function Working({ reasons, heading }) {
  return (
    <>
      <div className="metric-label">{heading}</div>
      <div style={{ marginTop: 8 }}>
        {reasons?.length
          ? reasons.map((reason) => <ReasonRow key={reason.label} reason={reason} />)
          : <span className="mono-dim">Not scored yet.</span>}
      </div>
    </>
  )
}

/**
 * Force-free graph render: accounts on a circle, edges as chords.
 * Nodes light up as the pointer approaches — proximity is computed in SVG
 * user units so it stays correct at any viewport size.
 *
 * An unscored account is drawn hollow. It used to be drawn green, because a
 * null bot likelihood fell through a `?? 0` and came out the colour of an
 * account the scorer had cleared.
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
          const scored = account.bot_likelihood !== null && account.bot_likelihood !== undefined
          const tone = riskTone(account.bot_likelihood)
          const active = near === account.id
          return (
            <g key={account.id}>
              <circle
                cx={x} cy={y} r={active ? 9 : 5.5}
                fill={scored ? tone : 'transparent'}
                stroke={tone}
                strokeWidth={scored ? 0 : 1.4}
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
        Hover a node to trace its edges · red = high bot likelihood · hollow = not scored
      </div>
    </div>
  )
}

/**
 * Clusters of accounts that all interact with the same accounts.
 *
 * The panel leads with the run, not the list. An empty campaign list and a
 * detector nobody has run look identical, and only one of them means there is
 * nothing there — so the latest pass, its notes, and what it declined to
 * report sit above the results rather than under them.
 */
function Campaigns() {
  const campaigns = useApiList('/api/social/campaigns/')
  const [run, setRun] = useState(null)
  const [running, setRunning] = useState(false)
  const [open, setOpen] = useState(null)
  const [error, setError] = useState(null)

  const loadRun = useCallback(
    () => apiFetch('/api/social/campaigns/latest_run/').then(setRun),
    [],
  )

  useEffect(() => {
    loadRun().catch((e) => setError(e.message))
  }, [loadRun])

  async function detect() {
    setRunning(true)
    setError(null)
    try {
      await apiFetch('/api/social/campaigns/detect/', { method: 'POST' })
      await loadRun()
      campaigns.reload()
    } catch (e) {
      setError(e.message)
    } finally {
      setRunning(false)
    }
  }

  const pass = run?.run || null
  const stale = run?.stale ?? 0

  return (
    <Panel title="Campaigns" tag="CLUSTER DETECTION">
      <div style={{ display: 'flex', gap: 20, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div style={{ minWidth: 150 }}>
          <div className="metric">{pass ? campaigns.rows.length : '—'}</div>
          <div className="metric-label">Clusters on record</div>
          <button
            type="button"
            className="btn ghost"
            style={{ marginTop: 10 }}
            disabled={running}
            onClick={detect}
          >
            {running ? 'Scanning…' : 'Run detection'}
          </button>
        </div>

        <div style={{ flex: 1, minWidth: 320 }}>
          {error && <div className="notice bad"><b>Detection failed.</b> {error}</div>}

          {!pass ? (
            <div className="notice">
              <b>The detector has not been run.</b> Nothing below is a finding
              yet — an empty list here means nobody has looked, which is not
              the same as nothing being there.
            </div>
          ) : (
            <>
              <div className="metric-label">
                Last pass {new Date(pass.created_at).toLocaleString()} ·{' '}
                {pass.accounts_considered} of {pass.accounts_total} accounts
                had enough targets to compare · {pass.detector_version}
              </div>
              <div style={{ marginTop: 10 }}>
                {pass.notes?.length
                  ? pass.notes.map((note) => <ReasonRow key={note.label} reason={note} />)
                  : <span className="mono-dim">Everything the pass looked at is listed below.</span>}
              </div>
              {stale > 0 && (
                <div className="mono-dim" style={{ fontSize: 11, lineHeight: 1.6 }}>
                  {stale} cluster{stale === 1 ? '' : 's'} below {stale === 1 ? 'was' : 'were'}
                  {' '}not found again by this pass. They are kept: the graph changing
                  underneath them is a fact about the capture, not a mistake to delete.
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {campaigns.rows.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 16 }}>
          <table>
            <thead>
              <tr>
                <th>Cluster</th>
                <th>Accounts</th>
                <th>Shared targets</th>
                <th>Cohesion</th>
                <th>Last seen</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {campaigns.rows.map((c) => {
                const missed = pass && c.last_seen_at < pass.created_at
                return (
                  <Fragment key={c.id}>
                    <tr style={{ opacity: missed ? 0.55 : 1 }}>
                      <td>{c.name}</td>
                      <td className="mono-dim">{c.size}</td>
                      <td className="mono-dim">{c.shared_target_count}</td>
                      <td style={{ minWidth: 140 }}><ScoreCell value={c.cohesion} /></td>
                      <td className="mono-dim">
                        {c.last_seen_at ? new Date(c.last_seen_at).toLocaleDateString() : '—'}
                        {missed ? ' · not in latest pass' : ''}
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button
                          type="button"
                          className="btn ghost"
                          onClick={() => setOpen(open === c.id ? null : c.id)}
                        >
                          {open === c.id ? 'Hide' : 'Why'}
                        </button>
                      </td>
                    </tr>
                    {open === c.id && (
                      <tr>
                        <td colSpan={6} style={{ background: 'rgba(0,0,0,0.35)' }}>
                          <div style={{ padding: '10px 0 14px' }}>
                            <Working reasons={c.reasons} heading="Why these accounts were grouped" />
                            <div className="metric-label" style={{ marginTop: 14 }}>Members</div>
                            <div style={{ marginTop: 6, fontSize: 12, lineHeight: 1.8 }}>
                              {c.members.map((m) => (
                                <span key={m.id} style={{ marginRight: 14 }}>
                                  @{m.handle}{' '}
                                  <span className="mono-dim">
                                    ({m.bot_likelihood === null ? 'not scored'
                                      : `bot ${m.bot_likelihood.toFixed(2)}`})
                                  </span>
                                </span>
                              ))}
                            </div>
                            <div className="metric-label" style={{ marginTop: 14 }}>
                              Accounts every member interacts with
                            </div>
                            <div className="mono-dim" style={{ marginTop: 6, fontSize: 12, lineHeight: 1.8 }}>
                              {c.common_targets.map((t) => `@${t.handle}`).join(' · ') || '—'}
                            </div>
                            <div className="mono-dim" style={{ marginTop: 14, fontSize: 11 }}>
                              {c.detector_version}
                            </div>
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
      )}
    </Panel>
  )
}

export default function Social() {
  const accounts = useApiList('/api/social/accounts/?ordering=-bot_likelihood')
  const edges = useApiList('/api/social/edges/')
  const posts = useApiList('/api/social/posts/')

  const [totals, setTotals] = useState(null)
  const [openAccount, setOpenAccount] = useState(null)
  const [openPost, setOpenPost] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  // The headline figures are counted in the database. The lists below are
  // paginated, so counting them here would report the first twenty-five of
  // everything as if it were everything.
  const loadTotals = useCallback(
    () => apiFetch('/api/social/accounts/summary/').then(setTotals),
    [],
  )

  useEffect(() => {
    loadTotals().catch(() => setTotals(null))
  }, [loadTotals])

  async function rescore() {
    setRefreshing(true)
    try {
      await apiFetch('/api/social/refresh-scores/', { method: 'POST' })
      accounts.reload()
      await loadTotals()
    } catch {
      /* the panel keeps showing the last good figures */
    } finally {
      setRefreshing(false)
    }
  }

  const unscored = totals?.accounts_unscored ?? 0

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Accounts" tag="MODULE 03">
          <div className="metric">{totals?.accounts ?? '—'}</div>
          <div className="metric-label">Tracked identities</div>
          <div className="sub-metrics">
            <div><b>{totals?.edges ?? '—'}</b>Edges</div>
            <div><b>{totals?.posts ?? '—'}</b>Posts</div>
          </div>
        </Panel>

        <Panel title="Flagged" tag={`BOT ≥ ${HIGH.toFixed(2)}`}>
          <div
            className="metric"
            style={{ color: totals?.flagged ? 'var(--magenta)' : undefined }}
          >
            {totals?.flagged ?? '—'}
          </div>
          <div className="metric-label">Suspected automation</div>
          {/* Shown beside the flag count, not below the fold. An account the
              scorer declined to judge is not a cleared account, and a panel
              that reports only flagged-of-total files it under "fine". */}
          <div
            className="mono-dim"
            style={{ marginTop: 10, fontSize: 11, lineHeight: 1.6 }}
          >
            {unscored
              ? `${unscored} account${unscored === 1 ? '' : 's'} carry no score — too `
                + 'little graph to judge on, not a clean bill of health.'
              : 'Every tracked account has a score.'}
          </div>
          <button
            type="button"
            className="btn ghost"
            style={{ marginTop: 10 }}
            disabled={refreshing}
            onClick={rescore}
          >
            {refreshing ? 'Rescoring…' : 'Rescore graph'}
          </button>
        </Panel>

        <Panel title="Interaction graph" tag="LIVE">
          <GraphMap accounts={accounts.rows} edges={edges.rows} />
        </Panel>
      </div>

      <Campaigns />

      <div style={{ height: 16 }} />

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
                  <th />
                </tr>
              </thead>
              <tbody>
                {accounts.rows.map((a) => (
                  <Fragment key={a.id}>
                    <tr>
                      <td>@{a.handle}</td>
                      <td className="mono-dim">{a.platform}</td>
                      <td style={{ minWidth: 150 }}><ScoreCell value={a.bot_likelihood} /></td>
                      <td style={{ minWidth: 150 }}><ScoreCell value={a.coordination_score} /></td>
                      <td className="mono-dim">{a.graph_model_version || '—'}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button
                          type="button"
                          className="btn ghost"
                          onClick={() => setOpenAccount(openAccount === a.id ? null : a.id)}
                        >
                          {openAccount === a.id ? 'Hide' : 'Why'}
                        </button>
                      </td>
                    </tr>
                    {openAccount === a.id && (
                      <tr>
                        <td colSpan={6} style={{ background: 'rgba(0,0,0,0.35)' }}>
                          <div style={{ padding: '10px 0 14px' }}>
                            <Working reasons={a.reasons} heading="How these scores were reached" />
                            <div className="mono-dim" style={{ marginTop: 12, fontSize: 11 }}>
                              {a.graph_model_version || 'unscored'}
                              {a.scored_at ? ` · last read ${new Date(a.scored_at).toLocaleString()}` : ''}
                            </div>
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
                  <th />
                </tr>
              </thead>
              <tbody>
                {posts.rows.map((p) => (
                  <Fragment key={p.id}>
                    <tr>
                      <td>@{p.handle}</td>
                      <td className="mono-dim" style={{ maxWidth: 420 }}>{p.text}</td>
                      <td className="mono-dim">
                        {p.sentiment === null || p.sentiment === undefined
                          ? <span title="No word in this post is in the lexicon">no match</span>
                          : p.sentiment.toFixed(2)}
                      </td>
                      <td style={{ minWidth: 140 }}><ScoreCell value={p.toxicity} /></td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button
                          type="button"
                          className="btn ghost"
                          onClick={() => setOpenPost(openPost === p.id ? null : p.id)}
                        >
                          {openPost === p.id ? 'Hide' : 'Why'}
                        </button>
                      </td>
                    </tr>
                    {openPost === p.id && (
                      <tr>
                        <td colSpan={5} style={{ background: 'rgba(0,0,0,0.35)' }}>
                          <div style={{ padding: '10px 0 14px' }}>
                            <Working reasons={p.reasons} heading="How these scores were reached" />
                            <div className="mono-dim" style={{ marginTop: 12, fontSize: 11 }}>
                              {p.text_model_version || 'unscored'}
                            </div>
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
    </div>
  )
}
