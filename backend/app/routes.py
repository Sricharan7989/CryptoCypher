"""
API routes for the VASP Attribution Engine.

Endpoints:
  /         - friendly landing page
  /health   - liveness probe
  /trace    - run (or replay) a forward trace and return the finding
  /report   - the same finding as an investigation-ready PDF
  /demos    - recorded traces available for instant replay
"""

from fastapi import APIRouter, HTTPException, Query, Response

from app import config
from core import tracer
from services import graph_store, replay, report
from services.etherscan import EtherscanError, is_valid_address

router = APIRouter()

# Most recent live payload per address, for this process only. Makes
# "trace, then download the PDF" instant instead of re-walking the chain.
_RECENT: dict[str, dict] = {}


@router.get("/health")
def health() -> dict:
    """
    Liveness probe — also what the frontend pings to confirm the backend is up.

    Reports which graph backend is live. Neo4j is optional: "memory" here is a
    healthy state, not a failure, and traces return identical results either way.
    """
    return {"status": "ok", "graph": graph_store.status()}


@router.get("/trace")
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


@router.get("/report")
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


@router.get("/demos")
def list_demos() -> dict:
    """
    Recorded traces available for instant replay.

    The frontend uses this to offer demo addresses that cannot fail on stage,
    while any other address still runs live.
    """
    return {"demos": replay.list_cached()}


@router.get("/")
def root() -> dict:
    """Friendly landing response so a bare localhost:8000 visit isn't a 404."""
    return {
        "service": "vasp-attribution-engine",
        "version": "0.1.0",
        # Reports only WHETHER a key is configured, never the key itself.
        "live_trace_available": config.has_etherscan_key(),
        "graph": graph_store.status(),
        "docs": "/docs",
    }
