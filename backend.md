# Backend — How It Works

## The Problem Being Solved

When cryptocurrency is stolen, the criminal cannot spend it directly. They must
move it to a **centralised exchange** (a VASP — Virtual Asset Service Provider)
that converts crypto into real money. Exchanges are regulated businesses: they
performed KYC (Know Your Customer) on the account holder and hold identity
records. If an investigator can prove that stolen funds reached a specific
exchange wallet, they can issue a **lawful data request** (via SAHYOG / I4C in
India) to obtain the criminal's real-world identity.

The backend solves the hard part of this process: **given a suspect's wallet
address, trace the stolen funds forward through the blockchain until they reach
an exchange, then identify which exchange it is and quantify how confident we
are in that identification.**

The criminal's own wallets are fresh, anonymous, and unrecognisable. But an
exchange is a massive, regulated business that cannot hide its on-chain
footprint. The backend exploits this asymmetry — it never tries to identify the
criminal. It follows the money until it arrives somewhere it *can* identify.

---

## Architecture Overview

```
backend/
├── main.py                    # Uvicorn entry point
├── app/                       # HTTP layer
│   ├── __init__.py            #   FastAPI factory + lifespan
│   ├── config.py               #   All configuration + secrets
│   └── routes.py              #   API endpoints
├── core/                      # Investigative engine
│   ├── tracer.py              #   Forward BFS trace
│   ├── identify.py            #   Exchange identification (4 methods)
│   └── scoring.py             #   Confidence scoring
└── services/                  # Data layer + integrations
    ├── etherscan.py           #   Blockchain data client
    ├── graph_store.py         #   Graph storage (Neo4j / NetworkX)
    ├── replay.py              #   Recorded trace replay
    └── report.py              #   PDF report generation
```

The backend is a **FastAPI** application. It has three layers:

| Layer | Package | Responsibility |
|-------|---------|----------------|
| **HTTP** | `app/` | Accept requests, validate input, return JSON or PDF |
| **Engine** | `core/` | The actual investigation: trace, identify, score |
| **Data** | `services/` | Talk to Etherscan, store the graph, cache results |

---

## Step-by-Step: What Happens When a Trace Runs

Below is the complete lifecycle of a single trace, from the moment the frontend
sends `GET /trace?address=0x...` to the JSON response it receives.

### Step 1 — Request Arrives at the API (`app/routes.py`)

The investigator (via the frontend) sends:

```
GET /api/trace?address=0x62425cd6...&max_depth=3&mode=auto
```

**What this step solves:** Validates the Ethereum address format (0x + 40 hex
characters) and rejects malformed input before any work begins. Determines
whether to serve a recorded replay, an in-memory recent result, or run a fresh
live trace.

**How it works:**

1. The `trace_address` route handler receives the query parameters.
2. `_run_or_replay()` is called, which checks three sources in order:
   - **Cache mode:** Does `data/cache/<address>.json` exist? If so, return it
     instantly. This is how demos work — pre-recorded traces that cannot fail on
     stage.
   - **In-memory recent:** Was this address traced earlier in this server process?
     If so, return that result (makes the "trace then download PDF" flow instant).
   - **Live trace:** Call into `core/tracer.py` with the Etherscan API.
3. If `mode=cache` and no recording exists → HTTP 404.
4. If no Etherscan API key is configured → HTTP 503.

**Why the mode system matters:** A live trace takes 2 seconds to 2 minutes and
depends on network conditions and API rate limits. In front of evaluators, a
slow network or a rate-limit trip would take the demo down. Replay mode
eliminates that risk while serving real data from a real trace — it just
doesn't re-fetch it.

---

### Step 2 — Configuration Is Loaded (`app/config.py`)

**What this step solves:** Centralises all secrets, paths, and tunables so that
nothing else in the codebase touches `os.environ`, and the frontend never sees
an API key.

**Key settings and why they exist:**

| Setting | Default | Purpose |
|---------|---------|---------|
| `ETHERSCAN_API_KEY` | *(from .env)* | Authenticates blockchain data requests |
| `MAX_TRACE_DEPTH` | 4 | Caps how many hops to follow (prevents combinatorial explosion) |
| `DUST_THRESHOLD_ETH` | 0.001 | Ignores tiny spam transfers |
| `MAX_EDGES_PER_NODE` | 25 | Per wallet, only expand the 25 largest outgoing flows |
| `MAX_NODES_PER_TRACE` | 400 | Hard ceiling on total wallets explored |
| `ETHERSCAN_REQUEST_DELAY_SEC` | 0.25 | Stay under the 5-calls/sec free tier |
| `LABELS_PATH` | `data/labels.json` | Address → entity mapping for known exchanges |
| `NEO4J_URI` | `bolt://localhost:7687` | Optional graph database |

**Why guard-rails are necessary:** Ethereum wallets have unbounded out-degree.
Without caps, a trace through a single exchange hot wallet could fan out to tens
of thousands of addresses. Each address costs an API call. An uncapped trace
never finishes and gets the API key rate-limited on the way.

---

### Step 3 — The Tracer Begins a Forward BFS Walk (`core/tracer.py`)

This is the heart of the system. The tracer's job: *follow the money forward
from the suspect wallet, hop by hop, until it reaches a place we recognise.*

**What this step solves:** Maps the path of stolen funds through anonymous
intermediary wallets to the regulated exit point.

#### 3a — Initialisation

```python
result = await tracer.trace(start_address, max_depth=3, dust_threshold=0.001)
```

1. The start address is normalised (lowercased, stripped).
2. A **graph store** is created — Neo4j if available, otherwise an in-memory
   NetworkX graph. Both implement the same interface and produce identical
   results; Neo4j just adds persistence.
3. The start wallet is added to the graph at depth 0.
4. A BFS queue is initialised: `[(start_address, depth=0)]`.

**Why BFS (breadth-first), not DFS (depth-first):** BFS visits wallets in order
of hop distance. The first time we reach any wallet, we have reached it by the
*shortest* path. When the result says "funds reached Binance 3 hops away", BFS
is what makes that number true — DFS could report a longer path as the first
one found.

#### 3b — The Main Loop (Forward Walk)

For each wallet in the queue:

```
while queue is not empty:
    address, depth = queue.popleft()

    1. Skip if already expanded (prevents infinite loops on cycles)
    2. Skip if depth >= max_depth (boundary of the trace)
    3. Skip if MAX_NODES_PER_TRACE reached (hard safety cap)

    4. Re-check identification (fan-in may have matured since queuing)
       → If identified as exchange/mixer/bridge: record it, stop this branch

    5. Fetch outgoing transfers from Etherscan
    6. Aggregate transfers by recipient (collapse 11 payments to same
       address into one flow carrying the summed value)
    7. Filter out dust (< 0.001 ETH per transaction)
    8. Rank by value, keep top MAX_EDGES_PER_NODE recipients
    9. For each recipient:
       a. Add to graph
       b. Record the hop (from → to, value, tx_hash)
       c. Check known labels (is this address in labels.json?)
       d. If identified as terminal (exchange/mixer/bridge): stop this branch
       e. Otherwise: enqueue for expansion at depth + 1
```

**Why we follow OUTGOING edges only:** At every wallet we ask "where did the
money go next", never "where did it come from". Following incoming edges would
walk backwards into the victims and earlier owners. An investigator already
knows the suspect — they need the *downstream* exit point where funds hit a
KYC'd business.

**Why dust filtering is per-transaction, not per-aggregate:** A thousand
0.0001 ETH spam sends total 0.1 ETH. Letting them sum past the threshold would
reopen the noise the threshold exists to shut out.

**Why aggregation happens before ranking:** If one recipient received 50 small
payments (totalling a lot of ETH) and another received one large payment, raw
transaction ranking would prefer the single large one. Aggregating first ensures
the *total flow* to each recipient is what determines importance.

#### 3c — Post-Walk Processing

After the BFS queue is empty:

1. **Final consolidation sweep:** Re-check every wallet in the graph for the
   deposit-consolidation pattern. Fan-in (how many wallets send to this one) is
   only fully known once the walk is over — a wallet where several branches
   converge may become recognisable here even though it looked ordinary mid-walk.

2. **Attach received values:** For each identified endpoint, sum the ETH that
   reached it along traced edges.

3. **Reconstruct paths:** For each attribution, find the shortest path from the
   suspect wallet to that endpoint and record which risky entity types (mixer,
   bridge) it crossed.

4. **Score confidence:** Pass each attribution through `scoring.py` (see Step 5).

5. **Collect risk flags:** Every labelled wallet that is a risk in its own right
   (mixer, bridge, scam, sanctioned) is flagged, with whether it sits on the
   primary path or elsewhere in the graph.

6. **Sort attributions:** Nearest first (lowest hop distance), then most
   confident. The closest exit point is the one an investigator should act on.

---

### Step 4 — Exchange Identification (`core/identify.py`)

**What this step solves:** Decides whether a wallet in the trace belongs to a
regulated business that holds KYC records. This is the step that makes the trace
*actionable* — a path through twelve anonymous wallets is useless on its own,
but the same path ending at "Binance" is one lawful request away from a human
identity.

**Why identification is possible at all:** A criminal's wallets are fresh and
anonymous. An exchange is the opposite: it serves millions of users, must
publish deposit addresses, and cannot cheaply rotate its infrastructure. It
cannot hide.

#### The Four Methods

| # | Method | Status | How It Works | Confidence |
|---|--------|--------|--------------|------------|
| **(a)** | **Known-label lookup** | ✅ Working | Exact match against `data/labels.json` — a curated register of exchange, mixer, and bridge addresses | 0.95 |
| **(b)** | **Consolidation pattern** | ✅ Working | Detects the fan-in fingerprint exchanges produce when sweeping customer deposits into hot wallets | 0.35–0.70 |
| **(c)** | Behavioral classifier | 🔲 Stub | Planned: classify wallets by transaction behaviour (frequency, amounts, timing) | — |
| **(d)** | Co-spend clustering | 🔲 N/A | Bitcoin technique; not applicable to Ethereum's account model | — |

**Method (a) — Known Labels:**

- Highest-quality signal. Exchange hot wallets are public knowledge.
- Confidence is 0.95, not 1.0: labels can go stale, exchanges rotate wallets.
- Labels are loaded from `data/labels.json`, which is watched for changes at
  runtime (no server restart needed).

**Method (b) — Consolidation Pattern:**

- An exchange gives every customer their own deposit address, then sweeps them
  all into a few hot wallets. This creates very high fan-in (many senders →
  one recipient), which is structurally unlike a personal wallet.
- Two gates must pass: at least 6 distinct senders (absolute floor) AND more
  than the 95th percentile of fan-in in this particular graph (adaptive floor).
- **Critical limitation:** A criminal re-pooling their own split funds produces
  the *same* fan-in signature. So this method yields `suspected_exchange`, never
  `exchange`. The report explicitly warns the investigator not to treat it as a
  named entity.

**Order matters:** Labels are checked first and returned immediately. The
consolidation heuristic only speaks when the label list is silent.

**Terminal entities:** Exchanges, suspected exchanges, mixers, and bridges are
all *terminal* — the trace stops expanding them. Exchanges because that's the
answer. Mixers because they deliberately sever the link between inputs and
outputs. Bridges because funds have left Ethereum.

---

### Step 5 — Confidence Scoring (`core/scoring.py`)

**What this step solves:** Quantifies how much weight an attribution deserves,
with a line-by-line explanation that an investigator, a defence lawyer, and a
judge can all interrogate.

**Why this is a weighted sum and NOT a machine learning model:** This number can
justify a legal request against a real person. Every point must be traceable to
a stated reason. A trained model that emitted "0.87" with no explanation would
be unchallengeable — the most dangerous property a figure can have in a legal
context.

#### The Three Factors

| Factor | What It Measures | Points |
|--------|-----------------|--------|
| **Identification method** | How the endpoint was recognised | +50 (label) or +22 (consolidation) |
| **Hop distance** | How many wallets separate suspect from endpoint | +30 at 1 hop, −7 per additional hop |
| **Path cleanliness** | What obfuscating infrastructure the money crossed | +15 (clean) / −30 (mixer) / −12 (bridge) |

**Score bounds:**
- **Maximum: 95** — Deliberate. Labels go stale, exchanges rotate wallets.
  A tool that can print "100% confident" invites someone to stop thinking.
- **Minimum: 5** — A recognised endpoint is always worth *something*.

**Example calculation:**

```
Direct label match           +50
1 hop from suspect           +30
No mixer or bridge on path   +15
                             ───
Total                         95 (capped at 95)
```

vs.

```
Consolidation pattern only   +22
3 hops from suspect          +16
Mixer on path                −30
                             ───
Total                          8 → clamped to 5 (minimum)
```

The API response carries the full breakdown so the panel can show the reasoning,
not just the number.

---

### Step 6 — Response Serialisation (`tracer.to_json()`)

**What this step solves:** Converts the engine's internal data structures into
the flat JSON shape the frontend consumes directly.

The response is structured as:

```json
{
  "start_address": "0x...",
  "params": { "max_depth": 3, "dust_threshold_eth": 0.001 },

  "summary": {
    "found": true,
    "exchange": "Binance",
    "address": "0x...",
    "hop_distance": 2,
    "confidence_score": 95,
    "confidence_breakdown": "direct label match (+50), 2 hops from suspect (+23), ...",
    "confidence_components": [...],
    "headline": "Funds reached Binance, 2 hops away, 95% confidence",
    "recommended_action": "Serve a lawful data request to Binance via SAHYOG..."
  },

  "attributions": [...],     // Every recognised address, nearest first
  "exchanges": [...],        // The actionable subset: VASPs that can be served
  "flags": [...],            // Mixers and bridges crossed on the way
  "risk_flags": [...],       // Detailed risk entries with severity

  "nodes": [...],            // Cytoscape-ready: { id, depth, is_start, label, ... }
  "edges": [...],            // { source, target, value_eth, tx_hash, ... }
  "hops": [...],             // Hops in order the walk made them

  "stats": {
    "nodes": 42,
    "edges": 67,
    "api_calls": 38,
    "cache_hits": 4,
    "elapsed_sec": 12.3,
    "graph_backend": "memory"
  },

  "notes": [...]             // Operational notes (truncation, errors)
}
```

**Why `nodes` and `edges` use `id` / `source` / `target`:** This is the shape
Cytoscape.js consumes directly. No reshaping needed in the browser.

---

### Step 7 — PDF Report Generation (`services/report.py`)

**What this step solves:** Produces a self-contained document an investigator
can attach to a SAHYOG request, hand to a supervisor, or disclose to a defence
lawyer.

Triggered by `GET /report?address=0x...`. Uses the exact same payload as
`/trace`, so the PDF can never contradict what was on screen.

**What the report contains:**

1. **Header:** Suspect address, generation date, data source, trace parameters
2. **Finding:** Headline result — confirmed exchange, unconfirmed lead, or
   nothing found
3. **Confidence assessment:** The full score arithmetic, factor by factor
4. **Traced path:** Hop-by-hop table with wallet roles, values, and transaction
   hashes anyone can verify on Etherscan
5. **Risk flags:** Mixers, bridges, sanctioned addresses encountered
6. **Recommended action:** What to do next
7. **Disclaimer:** Basis-and-limitations statement explaining what the tool did
   and did not establish

---

## Supporting Services

### Etherscan Client (`services/etherscan.py`)

**Problem solved:** Fetches a wallet's outgoing transaction history from the
public blockchain via the Etherscan API V2.

**Key design decisions:**

- **In-memory cache:** Criminal flows loop and re-converge. Without caching, the
  same wallet would be refetched once per inbound path. The cache is keyed by
  lowercased address and lives for the process lifetime.
- **Request throttle:** Serialises requests with a 250ms delay to stay under the
  5-calls/sec free tier. A rate-limit trip mid-demo is far worse than a trace
  that takes an extra second.
- **Etherscan's error model:** Reports failures with HTTP 200 and `status=0`.
  The client inspects the body rather than trusting the status code.
- **Filtering:** Drops failed transactions, contract creations, self-sends, and
  zero-value calls (which are typically ERC-20 interactions whose real value
  lives in the token contract).

### Graph Store (`services/graph_store.py`)

**Problem solved:** Stores the money-flow graph during a trace and answers
structural queries (shortest path, fan-in counts).

**Dual-backend design:**

- **Neo4j** (preferred): Persistent, supports cross-case analysis ("which other
  investigations touched this wallet?"), traversal in Cypher. Writes are
  buffered and flushed in batch to avoid per-transfer round trips.
- **NetworkX** (fallback): In-memory, no failure mode, faster for small graphs.
  Used automatically when Neo4j is unreachable.

Both implement the same interface. The tracer doesn't know which one it's using.
Neo4j is optional so a down container never takes the tool down.

### Replay Service (`services/replay.py`)

**Problem solved:** Serves pre-recorded traces instantly from
`data/cache/<address>.json`. The frontend's demo picker uses this to offer
addresses that cannot fail on stage.

Recordings carry `source: "cache"` and `recorded_at` so the UI can honestly
label them. A demo that silently pretends to be live would be dishonest.

---

## API Endpoints Summary

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /` | — | Service info + liveness check |
| `GET /health` | — | Liveness probe, reports graph backend status |
| `GET /trace?address=0x...` | Trace | Run or replay a forward trace, return JSON |
| `GET /report?address=0x...` | Report | Same finding as a downloadable PDF |
| `GET /demos` | List | Recorded traces for the demo picker |

---

## User Flow (Backend Perspective)

```
Investigator enters suspect address in UI
          │
          ▼
    GET /trace?address=0x...&max_depth=3
          │
          ▼
    Validate address format ──── invalid? → HTTP 400
          │
          ▼
    Check replay cache ──── found? → return instantly
          │
          ▼
    Check Etherscan API key ──── missing? → HTTP 503
          │
          ▼
    Start BFS walk from suspect wallet
    ┌─────────────────────────────────────┐
    │  For each wallet in queue:          │
    │    1. Fetch outgoing transactions   │
    │    2. Aggregate by recipient        │
    │    3. Filter dust, rank by value    │
    │    4. Identify each recipient:      │
    │       - Label match? → record it    │
    │       - Consolidation? → flag it     │
    │       - Terminal? → stop branch     │
    │       - Unknown? → queue it         │
    └─────────────────────────────────────┘
          │
          ▼
    Post-walk: consolidation sweep, path reconstruction, scoring
          │
          ▼
    Serialise to JSON (Cytoscape-ready nodes + edges)
          │
          ▼
    Return to frontend with summary, attributions, risk flags
          │
          ▼
    (optional) GET /report → same data rendered as PDF
```
