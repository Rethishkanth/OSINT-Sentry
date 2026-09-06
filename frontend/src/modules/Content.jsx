import { Fragment, useState } from 'react'

import { apiFetch } from '../api'
import Panel from '../components/Panel'
import { Badge, DataState, TenPointRating } from '../components/common'
import { useApiList } from '../hooks/useApiList'

// Colour ramp for the 0-10 rating. Deliberately not a red/green traffic
// light: this measures corroboration, and a confident green tick would be
// read as a fact-check verdict, which it is not.
const VERDICT_TONE = {
  trustworthy: 'var(--cyan)',
  likely_reliable: 'var(--cyan)',
  partially_corroborated: 'var(--yellow)',
  uncorroborated: 'var(--yellow)',
  do_not_trust: 'var(--magenta)',
  unavailable: 'var(--text-dim)',
}

/** The corroboration rating out of 10, with its tag. */
function Rating({ report, size = 22 }) {
  return (
    <TenPointRating
      score={report.score}
      label={report.verdict_label}
      tone={VERDICT_TONE[report.verdict] || 'var(--text)'}
      size={size}
    />
  )
}

function timeOf(iso) {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

/** The log of credibility checks. Newest at the top — that is the one you just ran. */
function CredibilityChecks({ reports }) {
  const [openId, setOpenId] = useState(null)

  return (
    <Panel title="Credibility checks" tag="NEWEST FIRST">
      <div className="notice" style={{ marginBottom: 12 }}>
        <b>This rates corroboration, not truth.</b> It asks whether independent
        outlets carried the same story, using keywords pulled from the article.
        A widely repeated falsehood corroborates well; a genuine scoop does not.
      </div>

      <DataState {...reports} empty="Nothing checked yet">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Checked</th>
                <th>Story</th>
                <th>Source</th>
                <th>Rating</th>
                <th>Corroborating</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {reports.rows.map((r) => (
                <Fragment key={r.id}>
                  <tr>
                    <td className="mono-dim" style={{ whiteSpace: 'nowrap' }}>
                      {timeOf(r.created_at)}
                    </td>
                    <td style={{ maxWidth: 300 }}>{r.content_title || r.content_url}</td>
                    <td className="mono-dim">{r.source_domain}</td>
                    <td style={{ minWidth: 210 }}><Rating report={r} size={18} /></td>
                    <td className="mono-dim">
                      {r.verdict === 'unavailable' ? '—' : `${r.independent_sources} outlet(s)`}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost"
                        onClick={() => setOpenId(openId === r.id ? null : r.id)}
                      >
                        {openId === r.id ? 'Hide' : 'Why'}
                      </button>
                    </td>
                  </tr>
                  {openId === r.id && (
                    <tr>
                      <td colSpan={6} style={{ background: 'rgba(0,0,0,0.35)' }}>
                        <div style={{ padding: '4px 0 10px' }}>
                          <div className="metric-label">Keywords searched</div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, margin: '6px 0 14px' }}>
                            {r.keywords?.length
                              ? r.keywords.map((k) => (
                                <span key={k} className="badge" style={{ color: 'var(--cyan)' }}>{k}</span>
                              ))
                              : <span className="mono-dim">none usable</span>}
                          </div>

                          <div className="metric-label">Reasoning</div>
                          <p style={{ fontSize: 12, lineHeight: 1.7, marginTop: 6 }}>{r.rationale}</p>

                          {r.matches?.length > 0 && (
                            <>
                              <div className="metric-label" style={{ marginTop: 10 }}>
                                Matching coverage
                              </div>
                              <div className="table-wrap" style={{ marginTop: 6 }}>
                                <table>
                                  <thead>
                                    <tr>
                                      <th>Outlet</th>
                                      <th>Match</th>
                                      <th>Their credibility</th>
                                      <th>Headline</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {r.matches.map((m) => (
                                      <tr key={m.id}>
                                        <td className="mono-dim">{m.domain}</td>
                                        <td>{Math.round(m.similarity * 100)}%</td>
                                        <td className="mono-dim">
                                          {m.credibility != null ? m.credibility.toFixed(0) : '—'}
                                        </td>
                                        <td style={{ maxWidth: 320 }}>
                                          <a href={m.url} target="_blank" rel="noreferrer noopener">
                                            {m.title || m.url}
                                          </a>
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            </>
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
  )
}

/** Read one page, or crawl one source's feed. Both run server-side, inline. */
function Fetcher({ sources, onDone }) {
  const [url, setUrl] = useState('')
  const [state, setState] = useState({ phase: 'idle', message: null })
  const busy = state.phase === 'working'

  async function run(body, working) {
    setState({ phase: 'working', message: working })
    try {
      const result = await apiFetch('/api/content/fetch/', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      onDone(result.id ?? null)
      // Reading a page also rates it, so lead with the rating — that is the
      // question the person was actually asking.
      const report = result.corroboration
      const verdict = report
        ? (report.score === null || report.score === undefined
          ? ` ${report.verdict_label} — ${report.rationale}`
          : ` Rated ${report.score.toFixed(1)}/10 — ${report.verdict_label},`
            + ` corroborated by ${report.independent_sources} other outlet(s).`)
        : ''
      setState({ phase: 'done', message: `${result.detail || 'Done.'}${verdict}` })
      return result
    } catch (err) {
      setState({ phase: 'error', message: err.message })
      return null
    }
  }

  async function readPage(event) {
    event.preventDefault()
    const target = url.trim()
    if (!target) return
    const result = await run({ url: target }, `Reading ${target}…`)
    if (result) setUrl('')
  }

  return (
    <Panel title="Read a page" tag="FETCH">
      <form onSubmit={readPage}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          <div className="field" style={{ flex: 1, marginBottom: 0 }}>
            <label htmlFor="fetch-url">Article address</label>
            <input
              id="fetch-url"
              type="text"
              value={url}
              disabled={busy}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.theguardian.com/world/2026/…"
              autoComplete="off"
              spellCheck="false"
            />
          </div>
          <button type="submit" className="btn" disabled={busy || !url.trim()}>
            {busy ? 'Reading…' : 'Read'}
          </button>
        </div>
      </form>

      <div className="mono-dim" style={{ marginTop: 10 }}>
        Registers the site as a source if it is new, scores it, checks
        robots.txt, then reads the page. A few seconds is normal — requests to
        one host are deliberately spaced out.
      </div>

      {sources.rows.length > 0 && (
        <>
          <div className="metric-label" style={{ marginTop: 18 }}>Or crawl a source&apos;s feed</div>
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table>
              <thead>
                <tr>
                  <th>Domain</th>
                  <th>Feed</th>
                  <th>Last result</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {sources.rows.map((s) => (
                  <tr key={s.id}>
                    <td>{s.domain}</td>
                    <td className="mono-dim" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {s.feed_url || '—'}
                    </td>
                    <td><Badge value={s.feed_status} /></td>
                    <td>
                      <button
                        type="button"
                        className="btn ghost"
                        disabled={busy}
                        onClick={() => run({ source: s.id }, `Crawling ${s.domain}…`)}
                      >
                        Crawl feed
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

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

export default function Content() {
  const items = useApiList('/api/content/?ordering=-created_at')
  const sources = useApiList('/api/sources/')
  const reports = useApiList('/api/content/corroborations/?ordering=-created_at')
  const [selectedId, setSelectedId] = useState(null)
  const [checking, setChecking] = useState(null)
  const [checkNote, setCheckNote] = useState(null)

  const selected = items.rows.find((i) => i.id === selectedId) || items.rows[0] || null

  async function checkCredibility(itemId) {
    setChecking(itemId)
    setCheckNote(null)
    try {
      const report = await apiFetch(`/api/content/${itemId}/corroborate/`, { method: 'POST' })
      await reports.reload()
      setCheckNote(
        report.score === null
          ? `${report.verdict_label}: ${report.rationale}`
          : `${report.score.toFixed(1)}/10 — ${report.verdict_label}.`,
      )
    } catch (err) {
      setCheckNote(err.message)
    } finally {
      setChecking(null)
    }
  }

  return (
    <div className="scan-in">
      <div className="grid" style={{ marginBottom: 16 }}>
        <Panel title="Ingested" tag="MODULE 02">
          <div className="metric">{items.rows.length}</div>
          <div className="metric-label">Items past the credibility gate</div>
        </Panel>
        <Panel title="Analyzed" tag="NLP">
          <div className="metric">
            {items.rows.filter((i) => i.status === 'analyzed').length}
          </div>
          <div className="metric-label">Entities and topics extracted</div>
        </Panel>
        <Panel title="Entities" tag="EXTRACTED">
          <div className="metric">
            {items.rows.reduce((sum, i) => sum + (i.entities?.length || 0), 0)}
          </div>
          <div className="metric-label">Named entity mentions</div>
        </Panel>
        <Panel title="Latest check" tag="CORROBORATION">
          {reports.rows.length ? (
            <>
              <Rating report={reports.rows[0]} />
              <div className="metric-label" style={{ marginTop: 12 }}>
                {reports.rows[0].content_title?.slice(0, 60) || 'Most recent check'}
              </div>
            </>
          ) : (
            <>
              <div className="metric">—</div>
              <div className="metric-label">Nothing checked yet</div>
            </>
          )}
        </Panel>
      </div>

      <Fetcher
        sources={sources}
        onDone={(newId) => {
          if (newId) setSelectedId(newId)
          items.reload()
          sources.reload()
          reports.reload()
        }}
      />

      <div style={{ height: 16 }} />

      <CredibilityChecks reports={reports} />

      {checkNote && <div className="notice" style={{ margin: '12px 0' }}>{checkNote}</div>}

      <div style={{ height: 16 }} />

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.2fr) minmax(0, 1fr)', gap: 16 }}>
        <Panel title="Content stream" tag="SELECT TO INSPECT">
          <DataState {...items} empty="Nothing ingested">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Source</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {items.rows.map((i) => (
                    <tr
                      key={i.id}
                      onClick={() => setSelectedId(i.id)}
                      style={{
                        cursor: 'pointer',
                        background: selected?.id === i.id ? 'rgba(252,238,10,0.07)' : undefined,
                      }}
                    >
                      <td>{i.title || <span className="mono-dim">{i.url}</span>}</td>
                      <td className="mono-dim">{i.source_domain}</td>
                      <td><Badge value={i.status} /></td>
                      <td>
                        <button
                          type="button"
                          className="btn ghost"
                          disabled={checking === i.id}
                          onClick={(e) => { e.stopPropagation(); checkCredibility(i.id) }}
                        >
                          {checking === i.id ? 'Checking…' : 'Check credibility'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataState>
        </Panel>

        <Panel title="Analysis" tag={selected ? `ITEM #${selected.id}` : 'NO SELECTION'}>
          {!selected ? (
            <div className="empty">// Select an item</div>
          ) : (
            <div>
              <div className="metric-label">Summary</div>
              <p style={{ fontSize: 12, lineHeight: 1.7, color: 'var(--text)' }}>
                {selected.summary || <span className="mono-dim">Not analyzed</span>}
              </p>

              <div className="metric-label" style={{ marginTop: 18 }}>Entities</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
                {selected.entities?.length ? (
                  selected.entities.map((e) => (
                    <span key={e.id} className="badge" style={{ color: 'var(--cyan)' }}>
                      {e.text} · {e.label} ×{e.mention_count}
                    </span>
                  ))
                ) : (
                  <span className="mono-dim">None found</span>
                )}
              </div>

              <div className="metric-label" style={{ marginTop: 18 }}>Topics</div>
              <div style={{ marginTop: 8 }}>
                {selected.topics?.length ? (
                  selected.topics.map((t) => (
                    <div
                      key={t.id}
                      style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}
                    >
                      <span style={{ minWidth: 110, fontSize: 12 }}>{t.label}</span>
                      <span className="bar" style={{ flex: 1 }}>
                        <i style={{ width: `${Math.min(100, t.weight * 400)}%` }} />
                      </span>
                      <span className="mono-dim">{t.weight.toFixed(3)}</span>
                    </div>
                  ))
                ) : (
                  <span className="mono-dim">None found</span>
                )}
              </div>

              <div className="mono-dim" style={{ marginTop: 18 }}>
                Analyzer: {selected.analyzer_version || '—'}
              </div>
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}
