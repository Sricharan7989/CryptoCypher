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

Endpoints:
  /health  - liveness probe
  /trace   - run (or replay) a forward trace and return the finding
  /report  - the same finding as an investigation-ready PDF
  /demos   - recorded traces available for instant replay

Run with:  uvicorn main:app --reload --port 8000   (from backend/)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

import config
import graph_store
import replay
import report
import tracer
from etherscan import EtherscanError, get_client, is_valid_address


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Release the shared httpx pool and the Neo4j driver when the server stops."""
    yield
    await get_client().aclose()
    graph_store.reset_driver()


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


# Most recent live payload per address, for this process only. Makes
# "trace, then download the PDF" instant instead of re-walking the chain.
_RECENT: dict[str, dict] = {}


@app.get("/health")
def health() -> dict:
    """
    Liveness probe — also what the frontend pings to confirm the backend is up.

    Reports which graph backend is live. Neo4j is optional: "memory" here is a
    healthy state, not a failure, and traces return identical results either way.
    """
    return {"status": "ok", "graph": graph_store.status()}


@app.get("/trace")
async def trace_address(
    address: str = Query(..., description="Suspect Ethereum address (0x...)"),
    max_depth: int = Query(
        config.MAX_TRACE_DEPTH, ge=1, le=6, description="How many hops to follow forward"
    ),
    dust_threshold: float = Query(
        config.DUST_THRESHOLD_ETH, ge=0.0, description="Ignore transfers below this many ETH"
    ),
    mode: str = Query(
        "auto",
        pattern="^(auto|live|cache)$",
        description="auto = replay if recorded else live; live = always fresh; cache = recorded only",
    ),
    save: bool = Query(False, description="Record this result for instant replay later"),
) -> dict:
    """
    Follow the money forward from a suspect wallet and return the flow graph.

    Returns a flat `nodes` + `edges` pair (Cytoscape-ready), `hops` in the order
    the walk made them, and the attribution results:

      summary       - the headline finding for the investigator panel.
      attributions  - every recognised address, nearest hop first.
      exchanges     - the actionable subset: VASPs that can be served a request.
      flags         - mixers and bridges crossed on the way.

    Confidence is never 1.0. `method` on every attribution says whether the name
    came from a published label or from a behavioural pattern, because those
    justify very different actions.

    max_depth is capped at 6 by the route, not by taste: out-degree compounds,
    so each extra hop multiplies both the graph size and the API calls.
    """
    payload = await _run_or_replay(
        address, max_depth=max_depth, dust_threshold=dust_threshold, mode=mode
    )

    if save and payload.get("source") != "cache":
        replay.save_trace(payload)
        payload["recorded"] = True

    return payload


async def _run_or_replay(
    address: str, *, max_depth: int, dust_threshold: float, mode: str
) -> dict:
    """
    Produce a trace payload, from the recording if there is one, else live.

    Shared by /trace and /report so a report can never disagree with the trace
    the investigator was looking at when they asked for it. Also keeps the most
    recent result of each address in memory, so generating the PDF straight
    after a trace costs nothing rather than re-walking the chain.
    """
    if not is_valid_address(address):
        raise HTTPException(
            status_code=400,
            detail=f"'{address}' is not a valid Ethereum address (expected 0x + 40 hex chars).",
        )

    key = address.strip().lower()

    if mode in ("auto", "cache"):
        cached = replay.load_trace(key)
        if cached is not None:
            return cached
        if mode == "cache":
            raise HTTPException(
                status_code=404,
                detail=f"No recorded trace for {key}. Run it live first, or use mode=auto.",
            )

    # In-memory result from earlier in this process - what /report normally hits.
    if mode == "auto":
        recent = _RECENT.get(key)
        if recent is not None and recent["params"]["max_depth"] == max_depth:
            return recent

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

    payload = tracer.to_json(result)
    payload["source"] = "live"
    _RECENT[key] = payload
    return payload


@app.get("/report")
async def trace_report(
    address: str = Query(..., description="Suspect Ethereum address (0x...)"),
    max_depth: int = Query(config.MAX_TRACE_DEPTH, ge=1, le=6),
    dust_threshold: float = Query(config.DUST_THRESHOLD_ETH, ge=0.0),
    mode: str = Query("auto", pattern="^(auto|live|cache)$"),
):
    """
    The same finding as /trace, rendered as an investigation-ready PDF.

    Served as an attachment so the browser saves it under a filename an
    investigator can file without renaming. Uses the identical payload /trace
    returns, so the document can never contradict what was on screen.
    """
    payload = await _run_or_replay(
        address, max_depth=max_depth, dust_threshold=dust_threshold, mode=mode
    )

    pdf = report.build_report(payload)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="{report.filename_for(payload["start_address"])}"'
        },
    )


@app.get("/demos")
def list_demos() -> dict:
    """
    Recorded traces available for instant replay.

    The frontend uses this to offer demo addresses that cannot fail on stage,
    while any other address still runs live.
    """
    return {"demos": replay.list_cached()}


@app.get("/")
def root() -> dict:
    """Friendly landing response so a bare localhost:8000 visit isn't a 404."""
    return {
        "service": "vasp-attribution-engine",
        "version": app.version,
        # Reports only WHETHER a key is configured, never the key itself.
        "live_trace_available": config.has_etherscan_key(),
        "graph": graph_store.status(),
        "docs": "/docs",
    }
