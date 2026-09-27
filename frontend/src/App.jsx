import { useCallback, useEffect, useState } from 'react'
import bundled from './data/snapshot.json'
import { API, useRun } from './useRun'
import RunConsole from './components/RunConsole'
import Pipeline from './components/Pipeline'
import Backlog from './components/Backlog'
import PullRequests from './components/PullRequests'
import Findings from './components/Findings'
import Safeguards from './components/Safeguards'
import Timeline from './components/Timeline'
import Models from './components/Models'

function stats(data) {
  const merged = data.pull_requests.filter((p) => p.state === 'merged').length
  const done = data.stories.filter((s) => s.status.toLowerCase() === 'done').length
  const blocking = data.findings.filter((f) => ['BLOCKER', 'MAJOR'].includes(f.severity)).length
  return [
    { n: data.stories.length, k: 'Stories' },
    { n: done, k: 'Done' },
    { n: data.pull_requests.length, k: 'Pull requests' },
    { n: merged, k: 'Merged' },
    { n: data.findings.length, k: 'Review findings' },
    { n: blocking, k: 'Blocking' },
  ]
}

/** Live run state, shaped like the snapshot so the same components render it.
 *
 * The retry counters come from `totals`, not from `state`: the ones in state
 * are per-story and reset at each new story, so a finished run would otherwise
 * report zero review rounds however many it actually took.
 */
function liveStats(state, totals) {
  const stories = state.stories || []
  return [
    { n: stories.length, k: 'Stories' },
    { n: stories.filter((s) => s.status === 'DONE').length, k: 'Done' },
    { n: state.pr ? 1 : 0, k: 'Open PR' },
    { n: totals.dev, k: 'Dev attempts' },
    { n: totals.review, k: 'Review rounds' },
    { n: totals.findings, k: 'Findings' },
  ]
}

function when(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString()
}

export default function App() {
  const run = useRun()
  const [config, setConfig] = useState(null)
  const [online, setOnline] = useState(null)
  // The bundled file is only a fallback for when the API is down. The live read
  // below is what the page normally shows, so clearing the Jira project empties
  // these panels instead of leaving the last build's backlog on screen.
  const [snap, setSnap] = useState(bundled)
  const [fresh, setFresh] = useState(false)
  const [loading, setLoading] = useState(false)

  const loadSnapshot = useCallback((force) => {
    setLoading(true)
    return fetch(`${API}/api/snapshot${force ? '?refresh=true' : ''}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
      .then((s) => {
        setSnap(s)
        setFresh(true)
      })
      .catch(() => setFresh(false))
      .finally(() => setLoading(false))
  }, [])

  // The dashboard is still useful without the API (it renders the bundled
  // snapshot), so a missing server is reported rather than treated as fatal.
  useEffect(() => {
    fetch(`${API}/api/config`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
      .then((c) => {
        setConfig(c)
        setOnline(true)
        loadSnapshot(false)
      })
      .catch(() => setOnline(false))
  }, [loadSnapshot])

  // A finished run has certainly changed Jira and GitHub, so re-read rather
  // than leaving the panels showing the state from before it started.
  useEffect(() => {
    if (run.status === 'done' || run.status === 'halted') loadSnapshot(true)
  }, [run.status, loadSnapshot])

  const live = run.state && run.status !== 'idle'
  const headline = live ? liveStats(run.state, run.totals) : stats(snap)
  const project = config || snap.project

  return (
    <div className="shell">
      <header className="masthead">
        <div>
          <h1>Autonomous Software Engineering Team</h1>
          <p>
            Five LangGraph agents take a business requirement to merged code: a
            Product Manager writes the backlog, a Solution Architect designs it,
            a human approves, then Developer, Code Reviewer and QA loop story by
            story. Jira and GitHub are the systems of record.
          </p>
          <div className="meta">
            {project.repo} · Jira {project.jira_project}
            {online === false && ' · API offline — showing the last build snapshot'}
            {online === true && ' · API connected'}
            {online === true && fresh && (
              <>
                {' · read '}
                {when(snap.generated_at)}
                <button
                  type="button"
                  className="refresh"
                  onClick={() => loadSnapshot(true)}
                  disabled={loading}
                >
                  {loading ? 'refreshing…' : 'refresh'}
                </button>
              </>
            )}
          </div>
        </div>
      </header>

      <div className="stats">
        {headline.map((s) => (
          <div className="stat" key={s.k}>
            <div className="n">{s.n}</div>
            <div className="k">{s.k}</div>
          </div>
        ))}
      </div>

      {online === false ? (
        <section>
          <h2>Run a requirement</h2>
          <div className="panel">
            <p className="hint" style={{ margin: 0 }}>
              The API is not running, so a requirement cannot be submitted from
              here. Start it with <code>python server.py</code> and reload.
            </p>
          </div>
        </section>
      ) : (
        <RunConsole run={run} config={config} />
      )}

      <Pipeline
        pipeline={snap.pipeline}
        timeline={snap.timeline}
        visited={live ? run.visited : null}
        activeNode={run.activeNode}
      />

      <section>
        <h2>Backlog</h2>
        <p className="hint">
          {live
            ? 'Written by the PM agent during this run, live from the graph state.'
            : `Jira project ${project.jira_project}, read live from the API.`}
        </p>
        <Backlog
          epics={live ? run.state.epics : snap.epics}
          stories={
            live
              ? run.state.stories.map((s) => ({
                  key: s.id,
                  summary: s.title,
                  status: s.status,
                }))
              : snap.stories
          }
        />
      </section>

      <div className="cols">
        <section>
          <h2>Pull requests</h2>
          <p className="hint">Only a Code Reviewer PASS merges one.</p>
          <PullRequests
            pulls={
              live && run.state.pr
                ? [
                    {
                      number: run.state.pr.id,
                      branch: run.state.branch_name || run.state.pr.story_id,
                      state: run.state.pr.status === 'MERGED' ? 'merged' : 'open',
                      url: run.state.pr.url,
                    },
                  ]
                : snap.pull_requests
            }
          />
        </section>

        <section>
          <h2>Model routing</h2>
          <p className="hint">Each agent runs on the provider suited to its call volume.</p>
          <Models models={config ? config.models : snap.models} />
        </section>
      </div>

      <section>
        <h2>Code review findings</h2>
        <p className="hint">
          A single BLOCKER or MAJOR fails the gate and returns the story to the
          Developer on the same pull request.
        </p>
        <Findings
          findings={
            live && (run.state.review_findings || []).length
              ? run.state.review_findings.map((f) => ({ ...f, pr: run.state.pr?.id ?? '—' }))
              : snap.findings
          }
        />
      </section>

      <section>
        <h2>Safety mechanisms</h2>
        <p className="hint">
          LangGraph compares these counters, never the agent whose loop they
          gate. On breach the run halts with state preserved.
        </p>
        <Safeguards safeguards={snap.safeguards} />
      </section>

      {!live && snap.timeline.length > 0 && (
        <section>
          <h2>Previous run timeline</h2>
          <p className="hint">Stage-by-stage trace of the last recorded run.</p>
          <Timeline events={snap.timeline} />
        </section>
      )}

      <footer>
        Live runs stream from <code>server.py</code>. The panels above are read
        from the real Jira project and GitHub repository each time this page
        loads, and again whenever a run finishes.
      </footer>
    </div>
  )
}
