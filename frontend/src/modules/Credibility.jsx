import { Fragment, useEffect, useRef, useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState, ScoreBar, TenPointRating } from '../components/common'
import { useApiList } from '../hooks/useApiList'

const SAFETY_TONE = {
  safe: 'var(--cyan)',
  mostly_safe: 'var(--cyan)',
  some_concerns: 'var(--yellow)',
  caution: 'var(--magenta)',
  unsafe: 'var(--magenta)',
  unknown: 'var(--text-dim)',
}

function localTime(iso) {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

/** The log of site safety inspections. Newest first. */
function SiteSafety({ inspections, showCleared, onToggleCleared, onClear, clearing }) {
  const [openId, setOpenId] = useState(null)
  const [confirming, setConfirming] = useState(false)

  return (
    <Panel
      title="Site safety"
      tag={showCleared ? 'CLEARED' : 'NEWEST FIRST'}
      actions={
        // Kept to two short buttons: the panel title is uppercase and widely
        // letter-spaced, so a third control wrapped the heading onto two lines.
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', whiteSpace: 'nowrap' }}>
          <button
            type="button"
            className="btn ghost"
            onClick={() => { setConfirming(false); onToggleCleared() }}
          >
            {showCleared ? 'Back to log' : 'Show cleared'}
          </button>
          {!showCleared && (
            confirming ? (
              <>
                <button
                  type="button"
                  className="btn"
                  disabled={clearing}
                  onClick={async () => { await onClear(); setConfirming(false) }}
                >
                  {clearing ? 'Clearing…' : 'Confirm'}
                </button>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setConfirming(false)}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn ghost"
                disabled={!inspections.rows.length}
                onClick={() => setConfirming(true)}
                title="Hide these inspections. Nothing is deleted."
              >
                Clear log
              </button>
            )
          )}
        </div>
      }
    >
      <div className="notice" style={{ marginBottom: 12 }}>
        <b>This inspects what the site serves, not whether it is malicious.</b>{' '}
        Transport security, certificate, response headers, advertising,
        trackers and clutter. It is not a malware scanner, and it does not run
        JavaScript - anything injected at runtime is not counted.
      </div>

      {confirming && !showCleared && (
        <div className="notice" style={{ marginBottom: 12 }}>
          <b>Clear the safety log?</b> The {inspections.rows.length} inspection(s)
          below are hidden, not deleted - <b>Show cleared</b> reads them back.
          The sites stay tracked and keep every entry in the assessment log.
        </div>
      )}

      {showCleared && (
        <div className="notice" style={{ marginBottom: 12 }}>
          <b>Cleared inspections.</b> Kept, not deleted - clearing hides the
          safety log and changes nothing else. Every site below is still a
          tracked source and still has its full assessment log.
        </div>
      )}

      <DataState
        {...inspections}
        empty={showCleared ? 'Nothing has been cleared' : 'No sites inspected yet'}
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Inspected</th>
                <th>Domain</th>
                <th>Safety</th>
                <th>HTTPS</th>
                <th>Ads</th>
                <th>Trackers</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {inspections.rows.map((r) => (
                <Fragment key={r.id}>
                  <tr>
                    <td className="mono-dim" style={{ whiteSpace: 'nowrap' }}>
                      {localTime(r.created_at)}
                    </td>
                    <td>{r.source_domain}</td>
                    <td style={{ minWidth: 200 }}>
                      <TenPointRating
                        score={r.score}
                        label={r.rating_label}
                        tone={SAFETY_TONE[r.rating]}
                        size={18}
                      />
                    </td>
                    <td>
                      <Badge value={r.uses_https ? 'yes' : 'no'} />
                    </td>
                    <td
                      className="mono-dim"
                      style={{ color: r.ad_networks > 3 ? 'var(--magenta)' : undefined }}
                    >
                      {r.ad_networks}
                    </td>
                    <td className="mono-dim">{r.trackers}</td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setOpenId(openId === r.id ? null : r.id)}
                      >
                        {openId === r.id ? 'Hide' : 'Detail'}
                      </button>
                    </td>
                  </tr>
                  {openId === r.id && (
                    <tr>
                      <td colSpan={7} style={{ background: 'rgba(0,0,0,0.35)' }}>
                        <div style={{ padding: '4px 0 10px' }}>
                          <p style={{ fontSize: 12, lineHeight: 1.7 }}>{r.summary}</p>

                          <div className="metric-label" style={{ marginTop: 12 }}>
                            How the rating was reached
                          </div>
                          <div style={{ marginTop: 6 }}>
                            {r.reasons?.length ? r.reasons.map((reason) => (
                              <div
                                key={reason.label}
                                style={{ display: 'flex', gap: 10, fontSize: 12, marginBottom: 4 }}
                              >
                                <span
                                  className="mono-dim"
                                  style={{
                                    minWidth: 48,
                                    textAlign: 'right',
                                    color: reason.points < 0 ? 'var(--magenta)' : 'var(--cyan)',
                                  }}
                                >
                                  {reason.points > 0 ? '+' : ''}{reason.points}
                                </span>
                                <span>{reason.label}</span>
                              </div>
                            )) : <span className="mono-dim">Nothing deducted.</span>}
                          </div>

                          <ThirdParties findings={r.findings} />

                          <div className="mono-dim" style={{ marginTop: 12 }}>
                            Checked {r.checked_url || '-'} · {r.engine_version}
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
  )
}

/** Exactly which ad networks and trackers were found, named. */
function ThirdParties({ findings }) {
  const parties = findings?.third_parties || []
  const cert = findings?.certificate
  return (
    <>
      {cert?.present && (
        <>
          <div className="metric-label" style={{ marginTop: 14 }}>Certificate</div>
          <div className="mono-dim" style={{ fontSize: 12, marginTop: 4 }}>
            {cert.valid
              ? `${cert.issuer || 'issuer unknown'} · ${cert.protocol || ''} · expires in ${cert.days_remaining} days`
              : `Invalid - ${cert.problem}`}
          </div>
        </>
      )}

      <div className="metric-label" style={{ marginTop: 14 }}>
        Ads, trackers and other third parties
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
        {parties.length ? parties.map((p) => (
          <span
            key={p.domain}
            className="badge"
            title={`${p.domain} - seen ${p.count}×`}
            style={{
              color: ['miners', 'intrusive_ads'].includes(p.category)
                ? 'var(--magenta)'
                : p.category === 'ads' ? 'var(--yellow)' : 'var(--cyan)',
            }}
          >
            {p.name}
          </span>
        )) : <span className="mono-dim">None recognised</span>}
      </div>

      {findings?.inline_trackers?.length > 0 && (
        <div className="mono-dim" style={{ marginTop: 8, fontSize: 12 }}>
          Inline: {findings.inline_trackers.join(', ')}
        </div>
      )}
    </>
  )
}

// The registry lookup runs on the worker, so a new source appears before its
// result does. Poll the one row rather than showing a score-shaped blank.
const POLL_INTERVAL_MS = 1500
const POLL_ATTEMPTS = 8

/** Registration age, in the largest unit that stays readable. */
function DomainAge({ days }) {
  if (days === null || days === undefined) {
    return <span className="mono-dim" title="Registry lookup has not resolved">-</span>
  }
  const label = days >= 365 ? `${Math.floor(days / 365)}y` : `${days}d`
  // Under six months is the band the scorer penalises; make it visible.
  const fresh = days < 180
  return (
    <span
      className="mono-dim"
      style={{ color: fresh ? 'var(--magenta)' : 'var(--text)' }}
      title={`Registered ${days} days ago`}
    >
      {label}
      {fresh && ' ⚠'}
    </span>
  )
}

/** Submit a website - a pasted article URL or a bare domain - for scoring. */
function AddSource({ onResolved, onInspect }) {
  const [value, setValue] = useState('')
  const [state, setState] = useState({ phase: 'idle', message: null })
  // Kept in a ref so an unmount mid-poll cannot set state on a dead component.
  // Set on mount as well as cleared on unmount: StrictMode runs effects
  // mount -> cleanup -> mount, so clearing alone leaves it false forever and
  // every poll silently bails.
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])

  /** Wait for the worker to write back a registry result for this source. */
  async function awaitLookup(id, domain) {
    for (let i = 0; i < POLL_ATTEMPTS; i += 1) {
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS))
      if (!live.current) return ''
      let row
      try {
        row = await apiFetch(`/api/sources/${id}/`)
      } catch {
        continue // a transient read failure should not end the wait
      }
      if (!live.current) return ''
      if (row.registry_status) {
        onResolved()
        return row.domain_age_days === null
          ? `${domain}: registry returned “${row.registry_status}” - scored with age unknown.`
          : `${domain}: registered ${Math.floor(row.domain_age_days / 365)} years ago.`
      }
    }
    // Not an error - the row exists, the answer just has not landed yet.
    onResolved()
    return `${domain} added. The registry lookup is still running - `
      + 'reload in a moment, or check the Celery worker is up.'
  }

  async function submit(event) {
    event.preventDefault()
    const raw = value.trim()
    if (!raw) return

    setState({ phase: 'working', message: 'Submitting…' })
    try {
      const result = await apiFetch('/api/sources/', {
        method: 'POST',
        body: JSON.stringify({ domain: raw }),
      })
      setValue('')
      onResolved()
      setState({ phase: 'working', message: `${result.detail} Inspecting the site…` })

      // Registration metadata and the safety inspection answer different
      // halves of "check this website", and neither waits on the other - so
      // run them together rather than making the person sit through both in
      // turn. Without the inspection this panel scored a site and left the
      // safety log empty, which reads as the log being broken.
      const [registry, safety] = await Promise.all([
        awaitLookup(result.id, result.domain),
        onInspect(result.id),
      ])
      if (!live.current) return
      // Both halves name the domain, which reads as a stutter once they are
      // one sentence - the first mention is enough.
      const prefix = `${result.domain}: `
      const tail = safety.startsWith(prefix) ? safety.slice(prefix.length) : safety
      setState({ phase: 'done', message: [registry, tail].filter(Boolean).join(' ') })
    } catch (err) {
      if (live.current) setState({ phase: 'error', message: err.message })
    }
  }

  const busy = state.phase === 'working'

  return (
    <Panel title="Check a website" tag="ADD SOURCE">
      <form onSubmit={submit}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label htmlFor="add-source">Website address</label>
            <input
              id="add-source"
              type="text"
              value={value}
              disabled={busy}
              onChange={(e) => setValue(e.target.value)}
              placeholder="https://www.bbc.co.uk/news/article-123   or just   bbc.co.uk"
              autoComplete="off"
              spellCheck="false"
            />
          </div>
          <button type="submit" className="btn" disabled={busy || !value.trim()}>
            {busy ? 'Checking…' : 'Check'}
          </button>
        </div>
      </form>

      <div className="mono-dim" style={{ marginTop: 10 }}>
        Paste a full link or a bare domain - it is reduced to the registrable
        domain, so a BBC article and news.bbc.co.uk both resolve to bbc.co.uk.
        The site is scored from its registration and inspected for safety in
        one go; both results land in the logs below.
      </div>

      {state.message && (
        <div
          className={`notice ${state.phase === 'error' ? 'bad' : ''}`}
          style={{ marginTop: 12 }}
        >
          {state.message}
        </div>
      )}
    </Panel>
  )
}

export default function Credibility() {
  const scores = useApiList('/api/credibility/?ordering=-created_at')
  const sources = useApiList('/api/sources/')
  const [showCleared, setShowCleared] = useState(false)
  const inspections = useApiList(
    `/api/credibility/inspections/?ordering=-created_at${showCleared ? '&cleared=1' : ''}`,
  )
  const [busy, setBusy] = useState(null)
  const [inspecting, setInspecting] = useState(null)
  const [clearing, setClearing] = useState(false)
  const [note, setNote] = useState(null)

  async function clearInspections() {
    setClearing(true)
    setNote(null)
    try {
      const result = await apiFetch('/api/credibility/inspections/clear/', {
        method: 'POST',
        body: JSON.stringify({}),
      })
      await inspections.reload()
      setNote(result.detail)
    } catch (err) {
      setNote(err.message)
    } finally {
      setClearing(false)
    }
  }

  /** Inspect one site and put the result at the top of the safety log.
   *  Returns the one-line summary so a caller can fold it into its own status
   *  message; pass `announce: false` when the caller shows that summary
   *  itself, or it appears twice on screen. Failure is reported through
   *  `note` rather than thrown, so one failed inspection cannot swallow a
   *  caller's other results. */
  async function inspect(sourceId, { announce = true } = {}) {
    setInspecting(sourceId)
    setNote(null)
    // A new inspection belongs to the live log; leave the archive view so the
    // result the analyst just asked for is the one on screen.
    const wasCleared = showCleared
    setShowCleared(false)
    try {
      const result = await apiFetch('/api/credibility/inspect/', {
        method: 'POST',
        body: JSON.stringify({ source: sourceId }),
      })
      // Leaving the archive view already refetches the live log. Calling
      // reload() as well would refetch the *cleared* path this render closed
      // over, and whichever landed last would win - sometimes hiding the
      // inspection just asked for behind the archive it replaced.
      if (!wasCleared) await inspections.reload()
      const message = result.score === null
        ? `${result.source_domain}: ${result.rating_label}. ${result.summary}`
        : `${result.source_domain}: ${result.score.toFixed(1)}/10 - ${result.rating_label}. ${result.summary}`
      if (announce) setNote(message)
      return message
    } catch (err) {
      setNote(err.message)
      return ''
    } finally {
      setInspecting(null)
    }
  }

  const resolved = sources.rows.filter((s) => s.domain_age_days !== null)
  const unresolved = sources.rows.filter((s) => s.domain_age_days === null)

  async function rescore(sourceId) {
    setBusy(sourceId)
    setNote(null)
    try {
      await apiFetch('/api/credibility/score/', {
        method: 'POST',
        body: JSON.stringify({ source: sourceId, wait: true }),
      })
      // Scoring also refreshes the cached registry data, so the sources table
      // (domain age, registrar) is stale too - reload both.
      await Promise.all([scores.reload(), sources.reload()])
      setNote('Scoring complete.')
    } catch (err) {
      setNote(err.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Tracked sources" tag="MODULE 01">
          <div className="metric">{sources.rows.length}</div>
          <div className="metric-label">Registered origins</div>
        </Panel>
        <Panel title="Assessments" tag="TOTAL">
          <div className="metric">{scores.rows.length}</div>
          <div className="metric-label">Credibility records</div>
        </Panel>
        <Panel title="High trust" tag="TIER">
          <div className="metric">
            {scores.rows.filter((s) => ['high', 'verified'].includes(s.trust_tier)).length}
          </div>
          <div className="metric-label">Scored high or verified</div>
        </Panel>
        <Panel title="Latest inspection" tag="SITE SAFETY">
          {/* While the cleared view is open the rows below are the archive,
              not the live log - reporting one of them as "latest" would be
              wrong. */}
          {inspections.rows.length && !showCleared ? (
            <>
              <TenPointRating
                score={inspections.rows[0].score}
                label={inspections.rows[0].rating_label}
                tone={SAFETY_TONE[inspections.rows[0].rating]}
              />
              <div className="metric-label" style={{ marginTop: 12 }}>
                {inspections.rows[0].source_domain} ·{' '}
                {inspections.rows[0].ad_networks} ad network(s),{' '}
                {inspections.rows[0].trackers} tracker(s)
              </div>
            </>
          ) : (
            <>
              <div className="metric">-</div>
              <div className="metric-label">
                {showCleared ? 'Viewing cleared log' : 'No site inspected yet'}
              </div>
            </>
          )}
        </Panel>
        <Panel title="Registry coverage" tag="RDAP">
          <div className="metric" style={{ color: resolved.length ? 'var(--cyan)' : 'var(--text-dim)' }}>
            {resolved.length}
            <span style={{ fontSize: 18, color: 'var(--text-dim)' }}>
              /{sources.rows.length}
            </span>
          </div>
          <div className="metric-label">Domains with a real registration date</div>
          {unresolved.length > 0 && (
            <div className="mono-dim" style={{ marginTop: 12 }}>
              {unresolved.length} unresolved - scored with age unknown, not as new
            </div>
          )}
        </Panel>
      </div>

      <AddSource
        onResolved={() => { scores.reload(); sources.reload() }}
        onInspect={(id) => inspect(id, { announce: false })}
      />

      <div style={{ height: 16 }} />

      <SiteSafety
        inspections={inspections}
        showCleared={showCleared}
        onToggleCleared={() => setShowCleared((v) => !v)}
        onClear={clearInspections}
        clearing={clearing}
      />

      <div style={{ height: 16 }} />

      {note && <div className="notice" style={{ marginBottom: 16 }}>{note}</div>}

      <div className="notice" style={{ marginBottom: 16 }}>
        <b>Registration metadata only.</b> Scores come from RDAP registry data -
        domain age, registrar, TLD - which separates a domain created last week
        from one created in 1994. It carries no information about whether the
        content is accurate, so metadata alone is capped below the VERIFIED tier.
      </div>

      <Panel title="Assessment log" tag="PROVENANCE TRACKED">
        <DataState {...scores} empty="No assessments yet">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Score</th>
                  <th>Tier</th>
                  <th>Model</th>
                  <th>Rationale</th>
                </tr>
              </thead>
              <tbody>
                {scores.rows.map((s) => (
                  <tr key={s.id}>
                    <td>{s.source_domain}</td>
                    <td style={{ minWidth: 140 }}>
                      <ScoreBar value={s.credibility_score} max={100} />
                    </td>
                    <td><Badge value={s.trust_tier} /></td>
                    <td className="mono-dim">{s.model_version}</td>
                    <td className="mono-dim" style={{ maxWidth: 380 }}>{s.rationale}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </DataState>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Sources" tag="TRIGGER SCORING">
        <DataState {...sources} empty="No sources registered">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Domain</th>
                  <th>Publisher</th>
                  <th>Domain age</th>
                  <th>Registry</th>
                  <th>Latest score</th>
                  <th>Active</th>
                  <th />
                  <th />
                </tr>
              </thead>
              <tbody>
                {sources.rows.map((s) => (
                  <tr key={s.id}>
                    <td>{s.domain}</td>
                    <td className="mono-dim">{s.publisher || '-'}</td>
                    <td><DomainAge days={s.domain_age_days} /></td>
                    <td title={s.registrar || 'Registrar unknown'}>
                      <Badge value={s.registry_status} />
                    </td>
                    <td style={{ minWidth: 140 }}>
                      <ScoreBar value={s.latest_credibility_score} max={100} />
                    </td>
                    {/* Not a tier - reusing 'verified' here read as a trust
                        claim next to the real tier column. */}
                    <td><Badge value={s.is_active ? 'active' : 'inactive'} /></td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost"
                        disabled={busy === s.id}
                        onClick={() => rescore(s.id)}
                      >
                        {busy === s.id ? 'Scoring…' : 'Rescore'}
                      </button>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost"
                        disabled={inspecting === s.id}
                        onClick={() => inspect(s.id)}
                        title="Fetch the homepage and rate how safe and cluttered it is"
                      >
                        {inspecting === s.id ? 'Inspecting…' : 'Inspect site'}
                      </button>
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
