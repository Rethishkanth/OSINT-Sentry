import { Fragment, useCallback, useEffect, useState } from 'react'

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
  disputed: 'var(--magenta)',
  do_not_trust: 'var(--magenta)',
  unavailable: 'var(--text-dim)',
}

const CLAIM_TONE = {
  agreed: 'var(--cyan)',
  disputed: 'var(--magenta)',
  unverified: 'var(--text-dim)',
}

/** Bar width for one topic, as a percentage of the strongest topic shown.
 *
 * Relative rather than absolute because the weight means different things
 * per analyzer: the heuristic reports a term's share of the document, which
 * lands around 0.02, while the transformer reports a keyphrase's cosine
 * similarity to it, which lands around 0.4. A fixed multiplier that suits
 * one draws every bar empty or every bar full for the other.
 */
function topicBarWidth(weight, topics) {
  const strongest = Math.max(...topics.map((t) => t.weight || 0))
  if (!strongest) return 0
  return Math.min(100, (weight / strongest) * 100)
}

/** One line of the score's working, with what it contributed out of 10. */
function ReasonRow({ reason }) {
  const { label, detail, effect } = reason
  const tone = effect == null
    ? 'var(--text-dim)'
    : effect < 0 ? 'var(--magenta)' : effect > 0 ? 'var(--cyan)' : 'var(--yellow)'

  return (
    <div style={{ display: 'flex', gap: 12, alignItems: 'baseline', marginBottom: 8 }}>
      {/* Fixed-width so the contributions line up as a column that visibly
          sums to the rating, rather than as prose with numbers in it. */}
      <span
        className="mono-dim"
        style={{ minWidth: 52, textAlign: 'right', color: tone, fontVariantNumeric: 'tabular-nums' }}
      >
        {effect == null ? '-' : `${effect > 0 ? '+' : ''}${effect.toFixed(2)}`}
      </span>
      <span style={{ fontSize: 12, lineHeight: 1.6 }}>
        <b style={{ color: 'var(--text)' }}>{label}.</b>{' '}
        <span style={{ color: 'var(--text-dim)' }}>{detail}</span>
      </span>
    </div>
  )
}

/** The article's own checkable specifics, and how they fared. */
function ClaimTable({ claims }) {
  return (
    <div className="table-wrap" style={{ marginTop: 6 }}>
      <table>
        <thead>
          <tr>
            <th>Article states</th>
            <th>Kind</th>
            <th>Result</th>
            <th>In context</th>
          </tr>
        </thead>
        <tbody>
          {claims.map((c, index) => (
            <tr key={`${c.kind}-${c.value}-${index}`}>
              <td style={{ whiteSpace: 'nowrap' }}>{c.display}</td>
              <td className="mono-dim">{c.kind_label}</td>
              <td style={{ color: CLAIM_TONE[c.status], minWidth: 190 }}>
                {c.status === 'agreed' && `Matched by ${c.agreed_by.join(', ')}`}
                {c.status === 'disputed' && c.disputed_by.map(([domain, theirs]) => (
                  <div key={domain}>{domain} says {theirs}</div>
                ))}
                {c.status === 'unverified' && 'Not found in the coverage read'}
              </td>
              <td style={{ maxWidth: 380, fontSize: 11, color: 'var(--text-dim)' }}>{c.text}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// Published fact-checks. Only `false` and `mixed` get a warning colour, and
// only on a close match - see FactChecks below for why that restraint matters.
const FACTCHECK_TONE = {
  false: 'var(--magenta)',
  mixed: 'var(--yellow)',
  true: 'var(--cyan)',
  unrated: 'var(--text-dim)',
}

// Four of the five fact-check states render as an empty list, and they mean
// four different things. Collapsing them into "no fact-checks found" tells an
// analyst that nobody has disputed a story when in fact nobody has looked.
const FACTCHECK_STATE = {
  disabled: { tone: 'var(--text-dim)', text: 'Fact-check lookup is switched off.' },
  no_key: { tone: 'var(--text-dim)', text: 'Fact-check lookup has no API key, so nothing was looked up.' },
  unavailable: { tone: 'var(--yellow)', text: 'The fact-check lookup did not complete.' },
  none_found: { tone: 'var(--text-dim)', text: 'No published fact-check covers this subject.' },
  found: { tone: 'var(--cyan)', text: 'Published fact-checks share this subject.' },
}

/**
 * Fact-checks published by other people about the same subject.
 *
 * This panel cites; it does not judge. The match is made on the article's
 * search terms, so a hit means "somebody has written about something like
 * this", and the only way to find out whether it addresses *this* article's
 * claim is to open it and read. The rating above is deliberately unaffected by
 * anything here, and the panel says so, because a reader who assumes the
 * number already accounts for a fact-checker's ruling will discount it twice.
 */
function FactChecks({ report }) {
  const state = FACTCHECK_STATE[report.fact_check_status] || FACTCHECK_STATE.disabled
  const rows = report.fact_checks || []
  const close = rows.filter((f) => f.close_match)
  const loose = rows.filter((f) => !f.close_match)

  return (
    <>
      <div className="metric-label" style={{ marginTop: 16 }}>
        Published fact-checks
      </div>
      <div className="mono-dim" style={{ marginTop: 4, fontSize: 11, lineHeight: 1.6 }}>
        <span style={{ color: state.tone }}>{state.text}</span>{' '}
        {report.fact_check_detail}
      </div>

      {rows.length > 0 && (
        <>
          <div className="mono-dim" style={{ marginTop: 8, fontSize: 11, lineHeight: 1.6 }}>
            These do not change the rating above, in either direction. They were
            matched on this story&apos;s search terms, and only reading one can tell
            you whether it reviewed this article&apos;s actual claim.
          </div>
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table>
              <thead>
                <tr>
                  <th>Fact-checker</th>
                  <th>Their rating</th>
                  <th>Match</th>
                  <th>Claim they reviewed</th>
                </tr>
              </thead>
              <tbody>
                {[...close, ...loose].map((f) => (
                  <tr key={f.id} style={{ opacity: f.close_match ? 1 : 0.6 }}>
                    <td className="mono-dim">{f.publisher || '-'}</td>
                    {/* Their words, not our bucket. "Pants on Fire" and
                        "Four Pinocchios" both bucket to false and both mean
                        something more specific than that. */}
                    <td style={{ color: FACTCHECK_TONE[f.verdict], whiteSpace: 'nowrap' }}>
                      {f.rating || <span className="mono-dim">no stated rating</span>}
                    </td>
                    <td className="mono-dim" style={{ whiteSpace: 'nowrap' }}>
                      {f.close_match
                        ? `close · ${Math.round(f.match_strength * 100)}%`
                        : `loose · ${Math.round(f.match_strength * 100)}%`}
                    </td>
                    <td style={{ maxWidth: 380, fontSize: 11 }}>
                      {f.url
                        ? <a href={f.url} target="_blank" rel="noreferrer noopener">{f.claim || f.title}</a>
                        : (f.claim || f.title)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {loose.length > 0 && (
            <div className="mono-dim" style={{ marginTop: 6, fontSize: 11, lineHeight: 1.6 }}>
              The dimmed rows share only some of this story&apos;s terms. They are
              leads to follow, not rulings on it.
            </div>
          )}
        </>
      )}
    </>
  )
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

/** The log of credibility checks. Newest at the top - that is the one you just ran. */
function CredibilityChecks({ reports }) {
  const [openId, setOpenId] = useState(null)

  return (
    <Panel title="Credibility checks" tag="NEWEST FIRST">
      <div className="notice" style={{ marginBottom: 12 }}>
        <b>This rates corroboration, not truth.</b> Each check searches a panel of
        major newsrooms - registered here or not - plus GDELT&apos;s global index,
        then <b>reads the closest matches in full</b> and compares the figures,
        sums and dates this article states against what they printed. A widely
        repeated falsehood still corroborates well; a genuine scoop still does not.
        Open <b>Why</b> on any row to see every part of the score and what it
        contributed.
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
                <th>Details checked</th>
                <th>Fact-checks</th>
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
                      {r.verdict === 'unavailable'
                        ? '-'
                        : `${r.independent_sources} outlet(s), ${r.outlets_read ?? 0} read`}
                    </td>
                    <td className="mono-dim">
                      {r.claims_checked
                        ? (
                          <>
                            <span style={{ color: CLAIM_TONE.agreed }}>{r.claims_agreed}</span>
                            {' / '}{r.claims_checked}
                            {r.claims_disputed > 0 && (
                              <span style={{ color: CLAIM_TONE.disputed }}>
                                {' '}({r.claims_disputed} conflict)
                              </span>
                            )}
                          </>
                        )
                        : '-'}
                    </td>
                    {/* Never a bare zero. "0" and "nobody looked" are the
                        same glyph and not the same finding. */}
                    <td className="mono-dim" style={{ whiteSpace: 'nowrap' }}>
                      {r.fact_check_status === 'found'
                        ? (
                          <span style={{
                            color: r.fact_checks_negative > 0 ? 'var(--magenta)' : 'var(--cyan)',
                          }}
                          >
                            {r.fact_checks_found} found
                            {r.fact_checks_negative > 0 && `, ${r.fact_checks_negative} negative`}
                          </span>
                        )
                        : r.fact_check_status === 'none_found'
                          ? 'none published'
                          : r.fact_check_status === 'unavailable'
                            ? <span style={{ color: 'var(--yellow)' }}>lookup failed</span>
                            : 'not looked up'}
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
                      <td colSpan={8} style={{ background: 'rgba(0,0,0,0.35)' }}>
                        <div style={{ padding: '4px 0 10px' }}>
                          <div className="metric-label">Keywords searched</div>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, margin: '6px 0 14px' }}>
                            {r.keywords?.length
                              ? r.keywords.map((k) => (
                                <span key={k} className="badge" style={{ color: 'var(--cyan)' }}>{k}</span>
                              ))
                              : <span className="mono-dim">none usable</span>}
                          </div>

                          <div className="metric-label">
                            Why this rating{r.score != null && ` - the parts add up to ${r.score.toFixed(1)}`}
                          </div>
                          <div style={{ margin: '10px 0 4px' }}>
                            {r.reasons?.length
                              ? r.reasons.map((reason, index) => (
                                <ReasonRow key={`${reason.label}-${index}`} reason={reason} />
                              ))
                              // Reports written by the v1 engine have no
                              // structured reasons, only the prose. Showing
                              // that is better than showing an empty box.
                              : <p style={{ fontSize: 12, lineHeight: 1.7 }}>{r.rationale}</p>}
                          </div>

                          {r.claims?.length > 0 && (
                            <>
                              <div className="metric-label" style={{ marginTop: 14 }}>
                                Details checked against the other coverage
                              </div>
                              <div className="mono-dim" style={{ marginTop: 4, fontSize: 11 }}>
                                Whether other outlets printed the same figures - not a ruling on
                                which is right. A correction looks exactly like an error from outside.
                              </div>
                              <ClaimTable claims={r.claims} />
                            </>
                          )}

                          <FactChecks report={r} />

                          {r.matches?.length > 0 && (
                            <>
                              <div className="metric-label" style={{ marginTop: 16 }}>
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
                                          {m.credibility != null ? m.credibility.toFixed(0) : '-'}
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
      // Reading a page also rates it, so lead with the rating - that is the
      // question the person was actually asking.
      const report = result.corroboration
      const verdict = report
        ? (report.score === null || report.score === undefined
          ? ` ${report.verdict_label} - ${report.rationale}`
          : ` Rated ${report.score.toFixed(1)}/10 - ${report.verdict_label},`
            + ` corroborated by ${report.independent_sources} other outlet(s)`
            + ` (${report.outlets_read ?? 0} read in full)`
            + (report.claims_checked
              ? `, ${report.claims_agreed}/${report.claims_checked} stated details matched`
                + (report.claims_disputed ? `, ${report.claims_disputed} conflicting` : '')
              : '')
            + '.')
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
        robots.txt, then reads the page. A few seconds is normal - requests to
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
                      {s.feed_url || '-'}
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
  // Counted server-side. The list endpoint pages at 25 and takes no page-size
  // parameter, so headline panels built from the rows on screen report 25 on
  // a corpus of any size above 25 - and look entirely plausible doing it.
  const [totals, setTotals] = useState(null)

  const loadTotals = useCallback(
    () => apiFetch('/api/content/summary/').then(setTotals).catch(() => setTotals(null)),
    [],
  )

  useEffect(() => { loadTotals() }, [loadTotals])

  const selected = items.rows.find((i) => i.id === selectedId) || items.rows[0] || null

  async function checkCredibility(itemId) {
    setChecking(itemId)
    setCheckNote(null)
    try {
      const report = await apiFetch(`/api/content/${itemId}/corroborate/`, { method: 'POST' })
      await reports.reload()
      await loadTotals()
      setCheckNote(
        report.score === null
          ? `${report.verdict_label}: ${report.rationale}`
          : `${report.score.toFixed(1)}/10 - ${report.verdict_label}.`,
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
          <div className="metric">{totals ? totals.items : '-'}</div>
          <div className="metric-label">Items past the credibility gate</div>
        </Panel>
        <Panel title="Analyzed" tag="NLP">
          <div className="metric">{totals ? totals.analyzed : '-'}</div>
          <div className="metric-label">Entities and topics extracted</div>
        </Panel>
        {/* The figure that changes what an analyst does next. An article
            nobody has cross-referenced is not a cleared article, and a panel
            showing only how many checks came back well files it as one. */}
        <Panel title="Never checked" tag="CORROBORATION">
          <div className="metric" style={{ color: totals?.unchecked ? 'var(--yellow)' : undefined }}>
            {totals ? totals.unchecked : '-'}
          </div>
          <div className="metric-label">
            {totals?.unchecked
              ? 'Ingested but never cross-referenced - unexamined, not cleared'
              : 'Every ingested item has been cross-referenced'}
          </div>
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
              <div className="metric">-</div>
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
          loadTotals()
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
                      {/* Only a model puts a number here. The heuristic has
                          nothing to say about how sure it is, and a blank is
                          the honest rendering of that. */}
                      {e.confidence == null ? '' : ` · ${Math.round(e.confidence * 100)}%`}
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
                        <i style={{ width: `${topicBarWidth(t.weight, selected.topics)}%` }} />
                      </span>
                      <span className="mono-dim">{t.weight.toFixed(3)}</span>
                    </div>
                  ))
                ) : (
                  <span className="mono-dim">None found</span>
                )}
              </div>

              <div className="mono-dim" style={{ marginTop: 18 }}>
                Analyzer: {selected.analyzer_version || '-'}
              </div>
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}
