export default function Safeguards({ safeguards }) {
  return (
    <div className="panel scroll">
      <table>
        <thead>
          <tr>
            <th style={{ width: 190 }}>Limit</th>
            <th style={{ width: 60 }}>Value</th>
            <th>Guards</th>
            <th>Observed in a live run</th>
          </tr>
        </thead>
        <tbody>
          {safeguards.map((s) => (
            <tr key={s.name}>
              <td className="key">{s.name}</td>
              <td style={{ fontVariantNumeric: 'tabular-nums' }}>{s.limit}</td>
              <td style={{ color: 'var(--muted)' }}>{s.guards}</td>
              <td style={{ color: 'var(--muted)' }}>{s.observed}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="note">
        <b>Every one of these fired during development.</b> Each halt preserved
        the full state — Jira keys, branch, PR, findings — so a human can resume.
        No run ever crashed, and no failing code was merged.
      </div>
    </div>
  )
}
