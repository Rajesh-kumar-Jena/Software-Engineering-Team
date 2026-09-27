export default function Models({ models }) {
  return (
    <div className="panel scroll">
      <table>
        <thead>
          <tr>
            <th>Agent</th>
            <th>Model</th>
            <th>Provider</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => (
            <tr key={m.agent}>
              <td className="key">{m.agent}</td>
              <td>{m.model}</td>
              <td>
                <span className="chip">{m.provider}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
