const CLASS = { merged: 'b-merged', open: 'b-open', closed: 'b-closed' }

export default function PullRequests({ pulls }) {
  if (pulls.length === 0) {
    return (
      <div className="panel">
        <p className="hint" style={{ margin: 0 }}>
          No pull requests yet — the Developer opens one per story.
        </p>
      </div>
    )
  }
  return (
    <div className="panel scroll">
      <table>
        <thead>
          <tr>
            <th style={{ width: 52 }}>PR</th>
            <th>Branch</th>
            <th style={{ width: 90 }}>State</th>
          </tr>
        </thead>
        <tbody>
          {pulls.map((p) => (
            <tr key={p.number}>
              <td className="key">
                <a href={p.url} target="_blank" rel="noreferrer"
                   style={{ color: 'inherit' }}>
                  #{p.number}
                </a>
              </td>
              <td className="key" style={{ color: 'var(--muted)' }}>{p.branch}</td>
              <td>
                <span className={'badge ' + (CLASS[p.state] || 'b-closed')}>
                  {p.state}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
