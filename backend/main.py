"""
FastAPI entry point for the Crypto Wallet -> VASP Attribution Engine (SIH26182).

WHY this service exists:
An investigator has one thing — a suspect Ethereum address. Criminals hide by
moving funds through many fresh, anonymous, self-controlled wallets, so trying
to recognise the criminal's own wallets is hopeless: they are brand new and
unlabelled. But to turn crypto into cash the funds must eventually land at a
centralised exchange (a VASP), which did KYC and cannot hide — it is a large
regulated business with a stable, recognisable on-chain fingerprint.

So this backend follows the money FORWARD through the public chain until it
reaches an exchange it recognises, and reports which one, how far away, and how
confident it is. Police then serve that exchange a lawful request via SAHYOG.
Nothing here breaks cryptography or unmasks anyone — it only reads public data.

Skeleton stage: only /health is implemented. The tracing engine, exchange
identification, scoring and PDF report land in later tasks.

Run with:  uvicorn main:app --reload --port 8000   (from backend/)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import config
import tracer
from etherscan import EtherscanError, get_client, is_valid_address


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Release the shared httpx connection pool when the server stops."""
    yield
    await get_client().aclose()


app = FastAPI(
    lifespan=lifespan,
    title="Crypto Wallet -> VASP Attribution Engine",
    description=(
        "Traces Ethereum funds forward from a suspect address to the "
        "centralised exchange where they land — the actionable chokepoint "
        "for a SAHYOG/I4C lawful request."
    ),
    version="0.1.0",
)

# The React + Vite frontend runs on a different origin during development, so
# the browser needs explicit permission to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    """Liveness probe — also what the frontend pings to confirm the backend is up."""
    return {"status": "ok"}


@app.get("/trace")
async def trace_address(
    address: str = Query(..., description="Suspect Ethereum address (0x...)"),
    max_depth: int = Query(
        config.MAX_TRACE_DEPTH, ge=1, le=6, description="How many hops to follow forward"
    ),
    dust_threshold: float = Query(
        config.DUST_THRESHOLD_ETH, ge=0.0, description="Ignore transfers below this many ETH"
    ),
) -> dict:
    """
    Follow the money forward from a suspect wallet and return the flow graph.

    Returns a flat `nodes` + `edges` pair (Cytoscape-ready) plus `hops` in the
    order the walk made them. At this stage every node's `label` is null - the
    tracer maps the money movement and makes no claim about who owns a wallet.
    Exchange attribution is a separate step.

    max_depth is capped at 6 by the route, not by taste: out-degree compounds,
    so each extra hop multiplies both the graph size and the API calls.
    """
    if not is_valid_address(address):
        raise HTTPException(
            status_code=400,
            detail=f"'{address}' is not a valid Ethereum address (expected 0x + 40 hex chars).",
        )

    if not config.has_etherscan_key():
        raise HTTPException(
            status_code=503,
            detail="ETHERSCAN_API_KEY is not configured in backend/.env - cannot run a live trace.",
        )

    try:
        result = await tracer.trace(
            address, max_depth=max_depth, dust_threshold=dust_threshold
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EtherscanError as exc:
        # Upstream problem, not the investigator's. 502 keeps that distinction
        # visible in the frontend instead of looking like an empty result.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return tracer.to_json(result)


@app.get("/")
def root() -> dict:
    """Friendly landing response so a bare localhost:8000 visit isn't a 404."""
    return {
        "service": "vasp-attribution-engine",
        "version": app.version,
        # Reports only WHETHER a key is configured, never the key itself.
        "live_trace_available": config.has_etherscan_key(),
        "docs": "/docs",
    }
