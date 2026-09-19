# Frontend — How It Works

## The Problem Being Solved

The backend traces stolen cryptocurrency through the blockchain and identifies
where it landed. But raw trace data — hundreds of wallet addresses, edge
weights, confidence floats, hop distances — is meaningless to the person who
needs it: a **police investigator** who has to decide what lawful action to
take next.

The frontend solves the presentation problem: **turn a complex graph traversal
result into a clear, investigator-facing answer to "where did the money go?"**

Everything in the frontend is written for a police officer, not a developer. No
method names, no field names, no raw confidence floats. The interface has one
job: state what was found, show the trail that proves it, be honest about how
certain it is, and offer the next lawful step.

---

## Architecture Overview

```
frontend/
├── index.html                 # Root HTML shell
├── vite.config.js              # Dev server + backend proxy
├── package.json               # Dependencies (React, Cytoscape.js)
└── src/
    ├── main.jsx               # React entry point
    ├── App.jsx                # Top-level state + layout
    ├── api.js                 # Backend communication layer
    ├── trace-path.js          # Path reconstruction + display helpers
    ├── styles.css             # Full application styling
    └── components/
        ├── TraceGraph.jsx     # Cytoscape.js money-flow visualisation
        └── FindingPanel.jsx   # Investigator-facing finding + actions
```

**Stack:** React 18 + Vite + Cytoscape.js. No state management library, no
router — the app is a single-screen investigation tool.

**Key design constraint:** The frontend never holds secrets. It talks to the
FastAPI backend through Vite's `/api` proxy, so the Etherscan API key never
reaches the browser. The backend is the only thing that touches external APIs.

---

## Module-by-Module Breakdown

### `vite.config.js` — Dev Server & Proxy

**Problem solved:** During development, the React dev server runs on `:5173` and
the FastAPI backend on `:8000`. The browser would block cross-origin requests
without CORS headers.

**How it contributes:**

```js
proxy: {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
    rewrite: (p) => p.replace(/^\/api/, ''),
  },
}
```

Every frontend request to `/api/trace?address=...` is silently forwarded to
`http://127.0.0.1:8000/trace?address=...`. This means:

1. No hardcoded host in the frontend code.
2. No CORS issues during development.
3. The API key stays on the server side — the browser only ever talks to its
   own origin.

---

### `api.js` — Backend Communication

**Problem solved:** Provides clean async functions for every backend call,
handles errors, and keeps HTTP details out of the UI components.

#### `runTrace(address, { maxDepth, mode })`

The core function. Builds the query string and calls `GET /api/trace`.

**How it contributes to the user flow:**

1. Constructs `URLSearchParams` with the address, depth, and mode.
2. Sends `fetch('/api/trace?...')` — proxied to the backend.
3. On a non-200 response, reads the FastAPI `detail` field from the JSON body
   and throws it as a human-readable `Error`. This matters because the
   investigator needs to know *why* a trace failed: "Invalid address" is
   different from "Etherscan API is down".
4. Returns the parsed JSON on success.

#### `listDemos()`

Fetches `GET /api/demos` — the list of pre-recorded traces available for
instant replay. Returns `[]` on any error, because the demo list is a
convenience and must never break the app.

#### `reportUrl(address, { maxDepth })`

Returns the URL string for `GET /api/report?address=...`. This is a plain link,
not a fetch: the backend already sends `Content-Disposition` with a
filed-ready filename, so the browser's native download handling gives the
investigator the right filename for free.

---

### `App.jsx` — Application Shell & State

**Problem solved:** Orchestrates the entire user interaction: address input,
demo selection, trace execution, and result display.

#### State Variables

| State           | Purpose                                                |
|-----------------|--------------------------------------------------------|
| `address`       | The wallet address the investigator typed              |
| `depth`         | How many hops to follow (2, 3, or 4)                   |
| `data`          | The full trace result from the backend (or `null`)     |
| `loading`       | Whether a trace is in progress                         |
| `error`         | Error message from a failed trace (or `null`)          |
| `toast`         | Temporary notification message                          |
| `demos`         | List of recorded demo traces                           |
| `forceLive`     | Whether to bypass recordings and query the chain fresh |

#### Layout Structure

```
┌─────────────────────────────────────────────────────┐
│  MASTHEAD                                           │
│  Brand + title | Search form + depth selector       │
├─────────────────────────────────────────────────────┤
│  DEMO BAR (if demos exist)                          │
│  Clickable chips for recorded traces                │
├───────────────────────────┬─────────────────────────┤
│                           │                         │
│  GRAPH COLUMN (58%)       │  FINDING PANEL (42%)    │
│  TraceGraph component     │  FindingPanel component │
│                           │                         │
├───────────────────────────┴─────────────────────────┤
│  TOAST (fixed, bottom center, temporary)             │
└─────────────────────────────────────────────────────┘
```

#### How a Trace Is Triggered

**From user input:**

1. Investigator types `0x62425cd6...` into the address field.
2. Selects hop depth (default: 3).
3. Clicks "Trace funds" or presses Enter.
4. `submit()` calls `trace(address)`.
5. `trace()` sets `loading=true`, clears previous `data` and `error`.
6. Calls `runTrace(address, { maxDepth, mode })`.
7. On success: `data` is set → graph and panel render.
8. On failure: `error` is set → panel shows the error message.
9. `loading` returns to false.

**From the demo bar:**

1. On mount, `useEffect` calls `listDemos()` to fetch available recordings.
2. Demo chips appear with labels like "Binance · 2 hops" and a shortened
   address.
3. Clicking a chip calls `runDemo(demo)`, which:
   - Sets the address in the input field.
   - Forces `mode=auto` (not live) so the recording is used.
   - Calls `trace()` with that address.

**The "Force live" toggle:** When checked, the `mode` parameter is set to
`"live"` instead of `"auto"`, bypassing any recording and querying the
blockchain fresh. Off by default so demos are fast and cannot break.

---

### `TraceGraph.jsx` — Money-Flow Visualisation

**Problem solved:** Renders the full graph of wallets and fund flows so the
investigator can see the *structure* of the laundering — how funds were split,
where they converged, and which path reaches the exchange.

**How it works, step by step:**

#### 1. Determine the Target and Highlight the Path

```js
const target = data.summary?.address ?? null
const path = target ? findPath(data.edges, data.start_address, target) : []
const onPath = new Set(pathAddresses(path, data.start_address))
const pathEdges = new Set(path.map((e) => `${e.source}->${e.target}`))
```

Before rendering, the component reconstructs the shortest path from the suspect
wallet to the headline finding (the nearest exchange). Every node and edge on
this path is flagged with `onPath = 1` so the stylesheet can highlight it.

**Why this matters:** The graph may contain dozens of wallets. Without the
highlighted path, the investigator would have to visually trace the route
themselves. The red highlighted trail shows *exactly* which wallets the
headline finding describes.

#### 2. Build Cytoscape Elements

Each backend `node` becomes a Cytoscape node with:

- **`kind`**: Determines colour and size. Computed by `nodeKind()`:
  - `start` → red, large (the suspect wallet)
  - `vasp` → green, large (confirmed exchange)
  - `suspect_vasp` → green outline (unconfirmed collection point)
  - `mixer` → orange (obfuscation service)
  - `bridge` → purple (cross-chain transfer)
  - `plain` → grey, small (anonymous intermediary)

- **`display`**: Only labelled entities and the start wallet get text. Labelling
  all 400 anonymous wallets would be noise, not information:
  - Start wallet → `"SUSPECT"`
  - Known exchange → its name (e.g. `"Binance"`)
  - Suspected exchange → `"collection point?"`
  - Everything else → empty string

Each backend `edge` becomes a Cytoscape edge with `onPath` flagging.

#### 3. Cytoscape Initialisation

```js
const cy = cytoscape({
  container: container.current,
  elements,
  style: STYLE,
  layout: {
    name: 'breadthfirst',    // ← matches the backend's BFS traversal order
    directed: true,
    roots: [data.start_address],
    spacingFactor: 1.1,
    padding: 24,
  },
  minZoom: 0.15,
  maxZoom: 3,
})
```

**Why breadthfirst layout:** The backend walks the graph breadth-first. Using a
matching layout means the visual structure mirrors the traversal — depth in the
graph corresponds to distance from the suspect, which is what hop distance
means.

#### 4. Node Interaction

Clicking any wallet node opens its Etherscan page in a new tab:

```js
cy.on('tap', 'node', (evt) => {
  window.open(`https://etherscan.io/address/${evt.target.id()}`, '_blank')
})
```

This lets the investigator verify any wallet's real transaction history without
leaving the tool.

#### 5. Legend

Below the graph, a legend shows what the colours mean plus a summary stat line:

> 42 wallets · 67 transfers · click a wallet to open Etherscan

#### Visual Encoding (Stylesheet)

| Element | Colour | Meaning |
|---------|--------|---------|
| 🔴 Red, large | `#dc2626` | Suspect wallet (starting point) |
| 🟢 Green, large | `#16a34a` | Confirmed exchange (the answer) |
| 🟢 Green outline | `#f0fdf4` + border | Unconfirmed collection point |
| 🟠 Orange | `#ea580c` | Mixer (chain of custody broken) |
| 🟣 Purple | `#7c3aed` | Bridge (funds left Ethereum) |
| ⚪ Grey, small | `#94a3b8` | Unhosted wallet (anonymous) |
| 🔴 Red edge | `#dc2626` | Part of the primary traced path |
| Grey edge | `#cbd5e1` | Other fund flows |

---

### `FindingPanel.jsx` — The Investigator's Answer

**Problem solved:** Presents the trace result as a structured, investigator-
facing finding — not raw data, but a clear statement of what was found, how
certain it is, and what to do next.

The panel renders one of **four states**, each designed for a different outcome:

#### State 1: Loading

```
┌──────────────────────────┐
│         ⟳                │
│   Following the money…   │
│                          │
│   Reading public records │
│   hop by hop. This can   │
│   take a minute on a     │
│   busy wallet.           │
└──────────────────────────┘
```

#### State 2: Error

```
┌──────────────────────────┐
│   Trace could not run    │
│                          │
│   [human-readable error  │
│    from backend detail]  │
└──────────────────────────┘
```

#### State 3: No Exchange Found (`summary.found=false, summary.lead=false`)

```
┌──────────────────────────┐
│  Finding                 │
│  No exchange reached     │
│                          │
│  The funds did not       │
│  arrive at any exchange  │
│  within 3 hops.          │
│                          │
│  ▸ Risk flags             │
│  ▸ What to do next       │
│  ▸ Download PDF report   │
└──────────────────────────┘
```

**Why this matters:** A negative result is still a result. The investigator may
need to widen the trace depth, expand the label database, or look for ERC-20
token movements.

#### State 4a: Unconfirmed Lead (`summary.found=false, summary.lead=true`)

```
┌──────────────────────────┐
│  Finding · unconfirmed    │
│  A collection point was  │
│  found 3 hops away       │
│                          │
│  [Confidence bar: 35%]    │
│                          │
│  ▸ Traced path           │
│  ▸ How identified:        │
│    Deposit-consolidation │
│  ▸ Risk flags             │
│  ▸ Recommended action:   │
│    DO NOT treat as an    │
│    exchange yet...       │
│  ▸ Download PDF report   │
└──────────────────────────┘
```

**Why the caution:** The consolidation pattern (many wallets funnelling into
one) looks the same whether it's an exchange sweeping deposits or a criminal
re-pooling their own split funds. The panel explicitly warns against acting on
it prematurely.

#### State 4b: Confirmed Exchange (`summary.found=true`)

```
┌──────────────────────────┐
│  Finding                 │
│  Funds reached Binance   │
│  2 hops away             │
│                          │
│  [Confidence bar: 95%]   │
│  123.45 ETH traced in    │
│                          │
│  ▸ Traced path           │
│  ▸ How identified:       │
│    Known exchange wallet │
│  ▸ Risk flags            │
│  ▸ Other exchanges       │
│  ▸ Recommended action:   │
│    Route to SAHYOG       │
│    [  Route to SAHYOG  ] │
│  ▸ Download PDF report   │
└──────────────────────────┘
```

#### Key Sub-Components

**`ConfidenceBar`** — Shows the confidence score as a percentage with a colour-
coded bar (green ≥85%, amber ≥60%, grey <60%). Has a "Why this score?" toggle
that expands to show the full scoring arithmetic from the backend:

```
direct label match              +50
2 hops from suspect             +23
no mixer or bridge on path      +15
                                ───
Confidence                       88
```

**Why this exists:** A confidence score that's just a number is unchallengeable.
Showing the arithmetic lets a supervisor or defence lawyer understand and argue
with each component.

**`TracedPath`** — The hop-by-hop route from suspect wallet to the endpoint:

```
● Suspect wallet                       hop 0
│ 0x6242…dc9f
│
● Intermediate wallet                  hop 1
│ 0x3a1b…7c2e
│ received 150.00 ETH across 3 transactions
│
● Binance                              hop 2
  0x28c6…955b
  received 148.23 ETH
```

Uses `findPath()` from `trace-path.js` to reconstruct the BFS shortest path on
the client side (the edge list already contains everything needed). Each wallet
is labelled with a human-readable role: "Suspect wallet", "Intermediate wallet",
"Possible collection point", or the exchange name.

**`RiskFlags`** — Sorted worst-first, with flags on the actual money trail
distinguished from those on side branches. A mixer the funds went through means
something different from one in the graph they didn't touch.

**`MethodNote`** — Explains in plain English how the identification was made:

- Known label: *"This address appears on our register of published exchange
  wallets. Exchanges cannot hide these."* (green border = strong signal)
- Consolidation: *"Many separate wallets funnel into this one address, which is
  how an exchange sweeps customer deposits. The exchange has not been named."*
  (amber border = weak signal)

**`ReportActions`** — A download link for the PDF report. Offered on every
outcome, including "nothing found" — a negative result is still a result an
investigator may need to file.

**`SourceBadge`** — When the result came from a cached recording, a badge states
this clearly with the capture timestamp. Honest labelling.

**SAHYOG routing button** — On a confirmed exchange finding, a button labelled
"Route to SAHYOG" simulates preparing a lawful data request. Clicking it:

1. Shows a toast notification: *"Request prepared for Binance — simulated
   SAHYOG routing, nothing was actually sent."*
2. Disables itself (one-click only).
3. The small print below explicitly states: *"Simulated for this demo — no
   request leaves this machine."*

---

### `trace-path.js` — Path Reconstruction & Display Helpers

**Problem solved:** The backend returns the full money-flow graph, but the
panel needs one specific thing: the chain of wallets from suspect to exchange.
This module derives it from the edge list without a server round-trip.

#### `findPath(edges, start, target)`

Breadth-first shortest path through the edges. Returns an array of edge objects
in order.

**Why BFS again:** The backend reports hop distance from a BFS walk. The path
shown to the investigator has to be the shortest one too, or it would contradict
the "N hops away" headline.

#### `pathAddresses(path, start)`

Extracts the ordered list of wallet addresses touched by the path (including the
start). Used by both the panel (for the hop-by-hop display) and the graph (for
highlighting).

#### `shortAddress(address)`

`0x62425cd6...dc9f` — Short enough to scan, long enough to compare by eye.

#### `describeRole(node, isStart)`

Translates backend labels into investigator language:

| Backend data | Display text |
|-------------|-------------|
| `is_start = true` | "Suspect wallet" |
| `entity_type = "suspected_exchange"` | "Possible collection point" |
| `is_vasp = true` | The entity name, or "Exchange" |
| `is_mixer = true` | "Tornado Cash · mixer" |
| `is_bridge = true` | "Stargate · bridge" |
| Everything else | "Intermediate wallet" |

#### `describeMethod(method)`

Returns `{ title, detail, strong }` for each identification method, in plain
English. The panel uses `strong` to choose a green or amber border colour.

#### `formatEth(value)`

Human-readable ETH values: `123,456 ETH` for large amounts, `1.23 ETH` for
moderate, `0.0012 ETH` for small.

---

### `styles.css` — Visual Design

**Design philosophy:** Calm, high-contrast, document-like. The finding is the
loudest thing on the screen; everything else stays quiet.

**Key design decisions:**

- **Colour carries meaning at a glance:** Red = suspect, green = exchange,
  orange = mixer, purple = bridge, grey = anonymous wallet. Consistent across
  the graph and the panel.
- **The finding header sets the tone:** Green gradient background when an
  exchange is found. Amber when there's only an unconfirmed lead. No colour for
  "nothing found".
- **Monospace for addresses:** Ethereum addresses are hex strings. Monospace
  makes them scannable and prevents characters from blending.
- **Responsive:** Below 1000px viewport width, the panel moves below the graph
  instead of beside it.

---

## Complete User Flow

```
INVESTIGATOR                        FRONTEND                            BACKEND
     │                                  │                                   │
     │  Types 0x62425cd6...             │                                   │
     │  Selects "3 hops"                │                                   │
     │  Clicks "Trace funds"            │                                   │
     │ ──────────────────────────────►  │                                   │
     │                                  │  fetch('/api/trace?address=...')   │
     │                                  │ ──────────────────────────────►   │
     │                                  │                                   │
     │  Sees spinner:                   │                                   │
     │  "Following the money…"          │      (BFS walk, 12 seconds)       │
     │                                  │                                   │
     │                                  │  ◄──── JSON response              │
     │                                  │        {summary, nodes, edges..}  │
     │                                  │                                   │
     │  Graph renders:                  │                                   │
     │  42 wallets, red path to         │                                   │
     │  green "Binance" node            │                                   │
     │                                  │                                   │
     │  Panel shows:                    │                                   │
     │  "Funds reached Binance"         │                                   │
     │  "2 hops away · 95%"            │                                   │
     │  Traced path with hashes         │                                   │
     │                                  │                                   │
     │  Clicks "Why this score?"        │                                   │
     │  Sees: label +50, hops +23,      │                                   │
     │  clean path +15 = 88 (→95 cap)   │                                   │
     │                                  │                                   │
     │  Clicks a wallet node            │                                   │
     │ ──────── opens Etherscan ──────► (external browser tab)              │
     │                                  │                                   │
     │  Clicks "Route to SAHYOG"        │                                   │
     │ ──────────────────────────────►  │  (simulated, no actual request)   │
     │  Toast: "Request prepared"       │                                   │
     │                                  │                                   │
     │  Clicks "Download PDF report"    │  fetch('/api/report?address=..') │
     │ ──────────────────────────────►  │ ──────────────────────────────►   │
     │  Browser saves PDF               │  ◄──── PDF bytes + filename       │
     │                                  │                                   │
```

---

## How the Frontend and Backend Agree

The frontend and backend are designed so they can never contradict each other:

| Aspect | How consistency is ensured |
|--------|--------------------------|
| **Path highlighting** | The frontend uses the same BFS algorithm as the backend to reconstruct the shortest path |
| **Node classification** | `nodeKind()` reads the same boolean flags (`is_vasp`, `is_mixer`, `is_bridge`) the backend sets |
| **Confidence display** | The score arithmetic comes from the backend, pre-computed. The frontend only renders it |
| **PDF report** | Generated from the exact same JSON payload that `/trace` returned, so the document can never contradict what was on screen |
| **Hop distance** | Both use BFS, so "N hops away" means the same thing in the headline, the path, and the graph layout |
| **Honest labelling** | If data is from a recording, both the `SourceBadge` and the graph stats say so |
