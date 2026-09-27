import { useCallback, useEffect, useRef, useState } from 'react'

export const API = import.meta.env.VITE_API ?? 'http://localhost:8000'

/** Nodes in execution order — also the order they are drawn. */
export const NODES = [
  'pm_agent',
  'solution_architect_agent',
  'human_approval_gate',
  'select_ready_story',
  'developer_agent',
  'code_reviewer_agent',
  'qa_agent',
]

/**
 * Drives one workflow run and keeps the live view in sync with it.
 *
 * The server is the source of truth: this subscribes to its Server-Sent Events
 * and derives everything the UI shows from them. On reconnect the server
 * replays the run from the beginning, so a refreshed page catches up rather
 * than showing a half-empty stream.
 */
export function useRun() {
  const [runId, setRunId] = useState(null)
  const [status, setStatus] = useState('idle')
  const [events, setEvents] = useState([])
  const [state, setState] = useState(null)
  const [awaiting, setAwaiting] = useState(null)
  const [activeNode, setActiveNode] = useState(null)
  const [visited, setVisited] = useState(() => new Set())
  const [error, setError] = useState(null)
  // Run totals. The counters in state are per-story and reset when the next
  // story is selected, so at the end of a run they all read zero even though
  // work happened. These bank each counter's value just before it resets, so
  // the headline numbers describe the whole run rather than its last moment.
  const [totals, setTotals] = useState({ dev: 0, review: 0, qa: 0, findings: 0 })
  const tally = useRef({ prev: { dev: 0, review: 0, qa: 0, findings: 0 },
                         banked: { dev: 0, review: 0, qa: 0, findings: 0 } })
  const source = useRef(null)
  // Highest sequence number already rendered. The server replays the whole run
  // to every new subscriber, and the browser reconnects a dropped EventSource
  // on its own -- without this the replay appends a second copy of the stream.
  const lastSeq = useRef(0)

  // Which agent is working right now: log lines name their node in brackets,
  // and a `node` event means that node just finished.
  const noteActivity = useCallback((event) => {
    if (event.type === 'log') {
      const match = /\[([a-z_]+)\]/.exec(event.message || '')
      if (match && NODES.includes(match[1])) {
        setActiveNode(match[1])
        setVisited((prev) => new Set(prev).add(match[1]))
      }
    } else if (event.type === 'node') {
      setVisited((prev) => new Set(prev).add(event.node))
      setActiveNode(null)
    }
  }, [])

  const accumulate = useCallback((state) => {
    const counters = state.counters || {}
    const current = {
      dev: counters.dev_attempts || 0,
      review: counters.review_iterations || 0,
      qa: counters.qa_iterations || 0,
      findings: (state.review_findings || []).length,
    }
    const { prev, banked } = tally.current
    for (const key of ['dev', 'review', 'qa', 'findings']) {
      // A drop means the counter was reset for a new story (or the findings
      // were cleared by a PASS); whatever it had reached is now history.
      if (current[key] < prev[key]) banked[key] += prev[key]
    }
    tally.current.prev = current
    setTotals({
      dev: banked.dev + current.dev,
      review: banked.review + current.review,
      qa: banked.qa + current.qa,
      findings: banked.findings + current.findings,
    })
  }, [])

  const subscribe = useCallback(
    (id) => {
      source.current?.close()
      const es = new EventSource(`${API}/api/runs/${id}/events`)
      source.current = es
      es.onmessage = (message) => {
        const event = JSON.parse(message.data)
        if (event.seq) {
          if (event.seq <= lastSeq.current) return // already rendered before a reconnect
          lastSeq.current = event.seq
        }
        setEvents((prev) => [...prev, event])
        noteActivity(event)

        if (event.state) {
          setState(event.state)
          accumulate(event.state)
        }
        if (event.type === 'awaiting_approval') {
          setAwaiting(event.payload)
          setStatus('awaiting_approval')
          setActiveNode('human_approval_gate')
        }
        if (event.type === 'decision') {
          setAwaiting(null)
          setStatus('running')
        }
        if (event.type === 'done') {
          setStatus(event.status)
          setActiveNode(null)
          es.close()
        }
        if (event.type === 'error') {
          setError(event.message)
          setStatus('error')
          setActiveNode(null)
          es.close()
        }
      }
      es.onerror = () => {
        // The browser retries automatically; only surface a hard failure once
        // the run has already finished and the stream is genuinely gone.
        if (es.readyState === EventSource.CLOSED) setStatus((s) => (s === 'running' ? 'error' : s))
      }
    },
    [noteActivity, accumulate],
  )

  const start = useCallback(
    async (requirement, dryRun) => {
      setEvents([])
      setState(null)
      setAwaiting(null)
      setError(null)
      setVisited(new Set())
      setStatus('running')
      lastSeq.current = 0
      setTotals({ dev: 0, review: 0, qa: 0, findings: 0 })
      tally.current = { prev: { dev: 0, review: 0, qa: 0, findings: 0 },
                        banked: { dev: 0, review: 0, qa: 0, findings: 0 } }
      try {
        const response = await fetch(`${API}/api/runs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ requirement, dry_run: dryRun }),
        })
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}))
          throw new Error(detail.detail || `server returned ${response.status}`)
        }
        const { run_id } = await response.json()
        setRunId(run_id)
        subscribe(run_id)
      } catch (exc) {
        setError(String(exc.message || exc))
        setStatus('error')
      }
    },
    [subscribe],
  )

  const decide = useCallback(
    async (approved, comments) => {
      if (!runId) return
      try {
        const response = await fetch(`${API}/api/runs/${runId}/decision`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ approved, comments, by: 'dashboard' }),
        })
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}))
          // Otherwise the button simply does nothing and the gate looks frozen.
          throw new Error(detail.detail || `server returned ${response.status}`)
        }
        setError(null)
      } catch (exc) {
        setError(`Could not submit the decision: ${exc.message || exc}`)
      }
    },
    [runId],
  )

  useEffect(() => () => source.current?.close(), [])

  return { runId, status, events, state, awaiting, activeNode, visited, error, totals, start, decide }
}
