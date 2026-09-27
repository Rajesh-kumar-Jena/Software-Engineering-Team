/**
 * Findings the Code Reviewer posted on real pull requests.
 *
 * Ordered by severity so the gating ones read first: a single BLOCKER or MAJOR
 * is enough to fail the review, while MINOR and INFO are reported without
 * blocking the merge.
 */
const RANK = { BLOCKER: 0, MAJOR: 1, MINOR: 2, INFO: 3 }

export default function Findings({ findings }) {
  if (findings.length === 0) {
    return (
      <div className="panel">
        <p className="hint" style={{ margin: 0 }}>No findings recorded.</p>
      </div>
    )
  }
  const sorted = [...findings].sort(
    (a, b) => (RANK[a.severity] ?? 9) - (RANK[b.severity] ?? 9) || a.pr - b.pr,
  )
  return (
    <div className="panel">
      {sorted.slice(0, 14).map((f, i) => (
        <div className="finding" key={i}>
          <div className="top">
            <span className={'badge b-' + f.severity.toLowerCase()}>
              {f.severity}
            </span>
            <span className="chip">{f.source}</span>
            <span className="loc">
              PR #{f.pr} · {f.file}
              {f.line ? `:${f.line}` : ''}
            </span>
            {f.rounds > 1 && (
              <span className="chip" title="restated by the reviewer across this many rounds">
                {f.rounds}× rounds
              </span>
            )}
          </div>
          <div className="msg">{f.message}</div>
        </div>
      ))}
      {sorted.length > 14 && (
        <p className="hint" style={{ margin: '12px 0 0' }}>
          + {sorted.length - 14} more across earlier pull requests.
        </p>
      )}
    </div>
  )
}
