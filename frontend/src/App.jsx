import { useState } from 'react'
import { runTrace } from './api.js'
import FindingPanel from './components/FindingPanel.jsx'
import TraceGraph from './components/TraceGraph.jsx'

// A real address with a clean, fast result - useful for a first run and for the
// demo, where waiting on a cold trace of a busy wallet is a bad look.
const SAMPLE = '0x62425cd6bdcb6bfe51558ea465b063486b70dc9f'

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

  const showToast = (message) => {
    setToast(message)
    setTimeout(() => setToast(null), 5000)
  }

  const submit = async (event) => {
    event.preventDefault()
    const target = address.trim()
    if (!target) return

    setLoading(true)
    setError(null)
    setData(null)
    try {
      setData(await runTrace(target, { maxDepth: depth }))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
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
          <button
            type="button"
            className="ghost"
            onClick={() => setAddress(SAMPLE)}
            disabled={loading}
          >
            Use sample
          </button>
        </form>
      </header>

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
