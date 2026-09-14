# CLAUDE.md — Crypto Wallet → VASP Attribution Engine (SIH26182)

This file is the project's north star. Read it before every task.

## What we are building

A lightweight blockchain-intelligence tool for a law-enforcement use case
(India's SAHYOG / I4C cybercrime workflow). An investigator enters a suspect
Ethereum wallet address. The tool traces the money forward, hop by hop, through
the public blockchain until it reaches a wallet controlled by a centralized
exchange (a VASP, e.g. Binance, WazirX). It then reports WHICH exchange the
funds reached, how many hops away, a confidence score, and any mixers/bridges
crossed on the way. That exchange is the actionable point: police serve it a
lawful request via SAHYOG, and the exchange's KYC records unmask the human.

The tool never breaks cryptography, never decrypts anything, never unmasks
anyone itself. It follows PUBLIC transaction data to a recognizable regulated
chokepoint. That is the whole idea.

## The core insight (state this in comments where relevant)

Criminals move stolen crypto through many fresh, anonymous, self-controlled
"unhosted" wallets to hide the trail. But to convert crypto into real cash they
must eventually deposit into a centralized exchange, which did KYC and can
freeze funds. We do NOT try to recognize the criminal's wallets (they are new
and anonymous). We follow the money THROUGH them until we recognize the
EXCHANGE at the end, which cannot hide because it is a large regulated business
with a stable, recognizable on-chain fingerprint.

## Scope discipline (IMPORTANT — do not over-build)

Priority order for this project:
1. A reliable, working forward trace that reaches an exchange.  <-- most important
2. A clear money-flow visualization.
3. Breadth: mixer flags, risk scoring.  <-- least important, only if time

Build the FULL skeleton, but only methods (a) known-label lookup and
(b) deposit-consolidation clustering must work WELL for exchange identification.
Methods (c) behavioral classifier and (d) co-spend clustering can be stubbed or
partially implemented and described as "future scope". Do NOT gold-plate.
Ethereum only. No multi-chain. No real-time streaming.

## Architecture

```
Frontend (React + Vite)
  - address input, "Trace" button
  - money-flow graph (Cytoscape.js)
  - finding panel: exchange, hops, confidence, flags, recommended action
        |
        v  (HTTP / JSON)
Backend (Python + FastAPI)
  - /trace endpoint
  - Tracing engine (NetworkX directed graph, forward BFS with depth cap)
  - Exchange identification module:
      a. known-label lookup   (WORKS — from labels file)
      b. consolidation cluster (WORKS — fan-in heuristic)
      c. behavioral classifier (STUB — future scope)
      d. co-spend clustering   (STUB / N/A on account model — future scope)
  - Risk + confidence scoring (weighted, explainable)
  - Report generator (PDF, later)
        |
        v
Data sources
  - Etherscan API (transaction history)  [ETHERSCAN_API_KEY]
  - Alchemy API (optional, cleaner transfers) [ALCHEMY_API_KEY]
  - labels.json (address -> entity: exchanges, mixers, bridges)
    seeded from Etherscan public labels + GraphSense TagPacks
```

## Tech stack (do not substitute without asking)

- Backend: Python 3.11+, FastAPI, uvicorn, httpx (async requests), networkx
- Data: pydantic models, python-dotenv for keys
- Frontend: React + Vite (JavaScript), Cytoscape.js for the graph
- PDF: reportlab (only when we reach that task)
- No database needed for the demo; in-memory + a labels.json file is fine.

## Hard rules

- API keys live ONLY in backend `.env` (gitignored). NEVER in frontend code.
- Cap trace depth (default 4) and ignore dust transfers (< a threshold) or the
  graph explodes. A single busy wallet can have tens of thousands of txns.
- Cache fetched wallets in memory during a trace so we never refetch.
- Respect Etherscan free-tier rate limit (5 calls/sec) — add small delays.
- Every attribution carries a confidence score. Never claim 100% certainty.
- Write clear docstrings explaining the WHY (this doubles as our PPT material).

## Demo reliability

We will demo with a known real scam/ransomware Ethereum address. Support a
cache/replay mode so the demo runs instantly and never depends on a live API
call succeeding mid-pitch, while still being able to do a fresh live trace on a
second address.

## Definition of done (for the finale)

Enter a real Ethereum address -> see an animated money-flow graph -> the path
lights up to a recognized exchange node -> a panel states
"Funds reached <Exchange>, N hops away, XX% confidence" with any mixer/bridge
flags -> a "Route to SAHYOG" button (simulated) and a downloadable PDF report.
