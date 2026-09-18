import { useEffect, useState } from 'react'
import { listDemos, runTrace } from './api.js'
import FindingPanel from './components/FindingPanel.jsx'
import TraceGraph from './components/TraceGraph.jsx'

function Toast({ message, onDismiss }) {
  if (!message) return null
  return (
    <div className="toast" role="status" onClick={onDismiss}>
      <span className="toast-check" aria-hidden="true">✓</span>
      <span>{message}</span>
    </div>
  )
}

export default function App() {
  const [address, setAddress] = useState('')
  const [depth, setDepth] = useState(3)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [toast, setToast] = useState(null)
  const [demos, setDemos] = useState([])
  // Live mode forces a fresh trace even for an address that has a recording.
  // Off by default so the demo is fast and cannot be broken by the network.
  const [forceLive, setForceLive] = useState(false)

  useEffect(() => {
    listDemos().then(setDemos)
  }, [])

  const showToast = (message) => {
    setToast(message)
    setTimeout(() => setToast(null), 5000)
  }

  const trace = async (target, { depth: useDepth = depth, live = forceLive } = {}) => {
    if (!target) return
    setLoading(true)
    setError(null)
    setData(null)
    try {
      setData(await runTrace(target, { maxDepth: useDepth, mode: live ? 'live' : 'auto' }))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const submit = (event) => {
    event.preventDefault()
    trace(address.trim())
  }

  const runDemo = (demo) => {
    setAddress(demo.address)
    setForceLive(false)
    trace(demo.address, { live: false })
  }

  return (
    <div className="app">
      <header className="masthead">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <h1>Wallet Attribution Engine</h1>
            <p>Trace crypto proceeds to the exchange that holds the KYC record</p>
          </div>
        </div>

        <form className="search" onSubmit={submit}>
          <input
            type="text"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Suspect wallet address (0x…)"
            spellCheck="false"
            aria-label="Suspect wallet address"
          />
          <select
            value={depth}
            onChange={(e) => setDepth(Number(e.target.value))}
            aria-label="How many hops to follow"
          >
            <option value={2}>2 hops</option>
            <option value={3}>3 hops</option>
            <option value={4}>4 hops</option>
          </select>
          <button type="submit" disabled={loading || !address.trim()}>
            {loading ? 'Tracing…' : 'Trace funds'}
          </button>
          <label className="live-toggle" title="Ignore recorded traces and query the chain now">
            <input
              type="checkbox"
              checked={forceLive}
              onChange={(e) => setForceLive(e.target.checked)}
              disabled={loading}
            />
            Force live
          </label>
        </form>
      </header>

      {demos.length > 0 && (
        <div className="demobar">
          <span className="demobar-label">Recorded demos — replay instantly:</span>
          {demos.map((demo) => (
            <button
              key={demo.address}
              type="button"
              className="demo-chip"
              onClick={() => runDemo(demo)}
              disabled={loading}
              title={demo.headline}
            >
              {demo.exchange
                ? `${demo.exchange} · ${demo.hop_distance} hops`
                : 'Unresolved trail'}
              <span className="demo-addr">{demo.address.slice(0, 10)}…</span>
            </button>
          ))}
        </div>
      )}

      <main className="workspace">
        <section className="graph-column">
          <TraceGraph data={data} />
        </section>
        <FindingPanel
          data={data}
          loading={loading}
          error={error}
          onToast={showToast}
        />
      </main>

      <Toast message={toast} onDismiss={() => setToast(null)} />
    </div>
  )
}
