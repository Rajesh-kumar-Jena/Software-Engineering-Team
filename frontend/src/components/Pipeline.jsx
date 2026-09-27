/**
 * The LangGraph graph as `core/graph.py` builds it.
 *
 * Nodes that appear in the run timeline are marked as having executed, so the
 * diagram doubles as a record of how far the last run actually got.
 */
export default function Pipeline({ pipeline, timeline, visited, activeNode }) {
  // During a live run the highlight tracks that run; otherwise it shows how far
  // the last recorded run got.
  const ran = visited ?? new Set(timeline.map((e) => e.node))

  // The linear spine, then the three feedback loops called out separately --
  // drawing every conditional edge inline turns the diagram into spaghetti.
  const spine = [
    'pm_agent',
    'solution_architect_agent',
    'human_approval_gate',
    'select_ready_story',
    'developer_agent',
    'code_reviewer_agent',
    'qa_agent',
  ]
  const byId = Object.fromEntries(pipeline.nodes.map((n) => [n.id, n]))

  return (
    <section>
      <h2>Pipeline</h2>
      <p className="hint">
        Agents make local decisions; LangGraph makes every routing decision.
        Highlighted nodes have executed; a pulsing border marks the one running now.
      </p>
      <div className="panel">
        <div className="flow">
          {spine.map((id, i) => {
            const node = byId[id]
            return (
              <div key={id}>
                <div className="flow-row">
                  <div
                    className={
                      'node' +
                      (ran.has(id) ? ' ran' : '') +
                      (activeNode === id ? ' active' : '') +
                      (id === 'human_approval_gate' ? ' gate' : '')
                    }
                  >
                    <div className="name">
                      {node.label}
                      {activeNode === id && <span className="node-live"> working…</span>}
                    </div>
                    <div className="role">{node.role}</div>
                    {node.tools.length > 0 && (
                      <div className="tools">
                        {node.tools.map((t) => (
                          <span className="chip" key={t}>
                            {t}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
                {i < spine.length - 1 && <div className="arrow">↓</div>}
              </div>
            )
          })}
        </div>

        <div className="loops">
          <div className="loop">
            <span className="tag fail">FAIL</span>
            <span>
              <b>Self-validation</b> — code does not run or parse → Developer
              retries locally, bounded by MAX_DEV_ATTEMPTS.
            </span>
          </div>
          <div className="loop">
            <span className="tag fail">FAIL</span>
            <span>
              <b>Code review</b> — findings go back to the Developer, who pushes
              to the <em>same</em> PR. Never a new one.
            </span>
          </div>
          <div className="loop">
            <span className="tag fail">FAIL</span>
            <span>
              <b>QA</b> — a Jira bug is filed and the Developer opens a{' '}
              <em>new</em> fix branch and PR, which re-enters review from the top.
            </span>
          </div>
          <div className="loop">
            <span className="tag pass">PASS</span>
            <span>
              <b>QA passes</b> — story marked DONE, then the backlog is re-scanned
              for the next READY story until none remain.
            </span>
          </div>
        </div>
      </div>
    </section>
  )
}
