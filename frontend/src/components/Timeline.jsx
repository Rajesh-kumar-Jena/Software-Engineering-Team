const LABEL = {
  pm_agent: 'PM',
  solution_architect_agent: 'Architect',
  human_approval_gate: 'Approval',
  select_ready_story: 'Select story',
  developer_agent: 'Developer',
  code_reviewer_agent: 'Reviewer',
  qa_agent: 'QA',
}

export default function Timeline({ events }) {
  if (events.length === 0) {
    return (
      <div className="panel">
        <p className="hint" style={{ margin: 0 }}>No run log available.</p>
      </div>
    )
  }
  return (
    <div className="panel">
      <div className="timeline">
        {events.map((e, i) => (
          <div className={'tl-row ' + e.level} key={i}>
            <span className="tl-node">{LABEL[e.node] || e.node}</span>
            <span className="tl-detail">{e.detail}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
