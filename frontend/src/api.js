// Talks to the FastAPI backend. Requests go through Vite's /api proxy, so no
// host is hardcoded here and the API key never comes near the browser.

export async function runTrace(address, { maxDepth = 4 } = {}) {
  const query = new URLSearchParams({ address, max_depth: String(maxDepth) })
  const response = await fetch(`/api/trace?${query}`)

  if (!response.ok) {
    // FastAPI puts the human-readable reason in `detail`. Surfacing it beats a
    // bare status code - an investigator needs to know whether they mistyped an
    // address or whether the chain data source is down.
    let detail = `Trace failed (HTTP ${response.status})`
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : detail
    } catch {
      // response had no JSON body; keep the status-based message
    }
    throw new Error(detail)
  }

  return response.json()
}
