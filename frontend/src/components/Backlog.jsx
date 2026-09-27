/** Status → badge class. Jira's workflow names vary by project template. */
function badge(status) {
  const s = status.toLowerCase()
  if (s === 'done') return 'b-done'
  if (s.includes('progress') || s.includes('review')) return 'b-prog'
  return 'b-todo'
}

export default function Backlog({ epics, stories }) {
  if (stories.length === 0) {
    return (
      <div className="panel">
        <p className="hint" style={{ margin: 0 }}>
          No issues in the project right now — the backlog is cleared between runs.
        </p>
      </div>
    )
  }
  return (
    <div className="panel scroll">
      {epics.length > 0 && (
        <table style={{ marginBottom: 18 }}>
          <thead>
            <tr>
              <th style={{ width: 110 }}>Epic</th>
              <th>Title</th>
            </tr>
          </thead>
          <tbody>
            {epics.map((e) => (
              <tr key={e.key}>
                <td className="key">{e.key}</td>
                <td>{e.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <table>
        <thead>
          <tr>
            <th style={{ width: 110 }}>Story</th>
            <th>Title</th>
            <th style={{ width: 120 }}>Status</th>
          </tr>
        </thead>
        <tbody>
          {stories.map((s) => (
            <tr key={s.key}>
              <td className="key">{s.key}</td>
              <td>{s.summary}</td>
              <td>
                <span className={'badge ' + badge(s.status)}>{s.status}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
