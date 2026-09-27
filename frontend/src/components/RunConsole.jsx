import { useEffect, useRef, useState } from 'react'
import { NODES } from '../useRun'

const LABELS = {
  pm_agent: 'Product Manager',
  solution_architect_agent: 'Solution Architect',
  human_approval_gate: 'Human Approval',
  select_ready_story: 'Select Story',
  developer_agent: 'Developer',
  code_reviewer_agent: 'Code Reviewer',
  qa_agent: 'QA',
}

const EXAMPLES = [
  'Build a coupon service that can create a coupon and apply it to an order',
  'Build a REST API endpoint for user profile retrieval',
  'Build a service that stores and retrieves customer notes',
]

/** One agent tile: idle, currently working, or already run. */
function AgentTile({ id, active, visited }) {
  const cls = active ? 'agent live' : visited ? 'agent done' : 'agent'
  return (
    <div className={cls}>
      <span className="dot" />
      <span className="agent-name">{LABELS[id]}</span>
      {active && <span className="agent-status">working…</span>}
    </div>
  )
}

export default function RunConsole({ run, config }) {
  const [requirement, setRequirement] = useState('')
  const [dryRun, setDryRun] = useState(true)
  const [comments, setComments] = useState('')
  const logEnd = useRef(null)

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: 'nearest' })
  }, [run.events.length])

  const busy = run.status === 'running' || run.status === 'awaiting_approval'
  const submit = (e) => {
    e.preventDefault()
    if (requirement.trim() && !busy) run.start(requirement.trim(), dryRun)
  }

  return (
    <section>
      <h2>Run a requirement</h2>
      <p className="hint">
        Describe a feature. The agents take it from backlog to merged pull
        request, pausing at the human approval gate for your decision.
      </p>

      <div className="panel">
        <form onSubmit={submit} className="composer">
          <textarea
            value={requirement}
            onChange={(e) => setRequirement(e.target.value)}
            placeholder="e.g. Build a coupon service that can create a coupon and apply it to an order"
            rows={3}
            disabled={busy}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit(e)
            }}
          />
          <div className="composer-bar">
            <label className="toggle" title="Simulate Jira, GitHub and SonarQube instead of writing to them">
              <input
                type="checkbox"
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
                disabled={busy}
              />
              Simulate tools (no real Jira / GitHub writes)
            </label>
            <button type="submit" disabled={busy || !requirement.trim()}>
              {busy ? 'Running…' : 'Start run'}
            </button>
          </div>
        </form>

        {!dryRun && !busy && (
          <div className="note" style={{ marginTop: 12 }}>
            <b>Live mode.</b> This will create real issues in Jira{' '}
            {config ? config.jira_project : ''} and real branches and pull
            requests in {config ? config.repo : 'the configured repository'}.
          </div>
        )}

        {run.status === 'idle' && (
          <div className="examples">
            {EXAMPLES.map((ex) => (
              <button key={ex} type="button" className="example" onClick={() => setRequirement(ex)}>
                {ex}
              </button>
            ))}
          </div>
        )}

        {run.status !== 'idle' && (
          <>
            <div className="agents">
              {NODES.map((id) => (
                <AgentTile
                  key={id}
                  id={id}
                  active={run.activeNode === id}
                  visited={run.visited.has(id)}
                />
              ))}
            </div>

            {run.state && (
              <div className="counters">
                <span>
                  status <b>{run.state.workflow_status || run.status}</b>
                </span>
                <span>
                  story <b>{run.state.current_story_id || '—'}</b>
                </span>
                <span>
                  dev <b>{run.state.counters.dev_attempts}</b>
                </span>
                <span>
                  review <b>{run.state.counters.review_iterations}</b>
                </span>
                <span>
                  qa <b>{run.state.counters.qa_iterations}</b>
                </span>
              </div>
            )}

            {run.awaiting && (
              <div className="approval">
                <div className="approval-head">Human approval required</div>
                <p>
                  The Solution Architect has refined{' '}
                  {run.awaiting.stories ? run.awaiting.stories.length : 0} stories.
                  No repository write can happen until you approve.
                </p>
                <ul className="approval-stories">
                  {(run.awaiting.stories || []).map((s) => (
                    <li key={s.id}>
                      <span className="key">{s.id}</span> {s.title}
                      <span className="chip" style={{ marginLeft: 8 }}>
                        {s.requires_ui ? 'UI + API' : 'backend only'}
                      </span>
                    </li>
                  ))}
                </ul>
                <input
                  className="approval-comment"
                  placeholder="Optional comments (sent to the Architect on reject)"
                  value={comments}
                  onChange={(e) => setComments(e.target.value)}
                />
                <div className="approval-actions">
                  <button className="approve" onClick={() => run.decide(true, comments)}>
                    Approve
                  </button>
                  <button className="reject" onClick={() => run.decide(false, comments)}>
                    Reject &amp; revise
                  </button>
                </div>
              </div>
            )}

            {run.error && <div className="error-box">{run.error}</div>}

            <div className="stream">
              {run.events
                .filter((e) => e.type === 'log' || e.type === 'node' || e.type === 'done')
                .slice(-160)
                .map((e, i) => (
                  <div className={'ev ' + (e.level || '')} key={e.seq ?? i}>
                    {e.type === 'log' ? (
                      <span className="ev-msg">{e.message}</span>
                    ) : e.type === 'node' ? (
                      <span className="ev-node">✓ {LABELS[e.node] || e.node} complete</span>
                    ) : (
                      <span className="ev-node">run {e.status}</span>
                    )}
                  </div>
                ))}
              <div ref={logEnd} />
            </div>
          </>
        )}
      </div>
    </section>
  )
}
