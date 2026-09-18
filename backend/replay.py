"""
Replay mode — serve a previously recorded trace instantly, from disk.

WHY THIS EXISTS
---------------
A live trace of a busy wallet makes up to a few hundred Etherscan calls against
a rate-limited free tier, and takes anywhere from two seconds to two minutes.
That is fine for investigative work and completely unacceptable in front of a
panel of judges: a slow network, a rate-limit trip or a flaky connection would
take the demo down at the worst possible moment, and none of it would say
anything about whether the tool works.

So a trace can be RECORDED once, to data/cache/<address>.json, and replayed
verbatim afterwards. The replayed payload is byte-for-byte the same JSON the
live path produces - it is the real result of a real trace, just not fetched
again. Nothing is faked; the only thing that changes is where the bytes come
from.

The recording carries `source` and `recorded_at` so the UI can say plainly that
it is showing a cached result. A demo that quietly pretends to be live would be
dishonest, and an investigator needs to know how fresh the data is.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import config

CACHE_DIR = config.DATA_DIR / "cache"


def _path_for(address: str) -> Path:
    """One file per address. Lowercased, so lookups never miss on casing."""
    return CACHE_DIR / f"{address.strip().lower()}.json"


def save_trace(payload: dict) -> Path:
    """
    Record a completed trace so it can be replayed instantly later.

    Stamps the payload with when it was captured. The stamp matters: a cached
    trace is a snapshot of the chain at a moment in time, and funds may well
    have moved since.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    address = payload.get("start_address", "")
    if not address:
        raise ValueError("Cannot cache a trace with no start_address")

    recorded = dict(payload)
    recorded["source"] = "cache"
    recorded["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recorded["recorded_epoch"] = int(time.time())

    path = _path_for(address)
    path.write_text(json.dumps(recorded, indent=2), encoding="utf-8")
    return path


def load_trace(address: str) -> dict | None:
    """The recorded trace for this address, or None if it was never recorded."""
    path = _path_for(address)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt recording must not take the demo down - fall through to live.
        return None
    payload["source"] = "cache"
    return payload


def has_trace(address: str) -> bool:
    return _path_for(address).exists()


def list_cached() -> list[dict]:
    """Every recorded trace, for the UI's demo picker."""
    if not CACHE_DIR.exists():
        return []

    entries = []
    for path in sorted(CACHE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        summary = payload.get("summary", {})
        entries.append(
            {
                "address": payload.get("start_address", path.stem),
                "recorded_at": payload.get("recorded_at"),
                "headline": summary.get("headline", ""),
                "exchange": summary.get("exchange"),
                "hop_distance": summary.get("hop_distance"),
                "confidence_score": summary.get("confidence_score"),
                "nodes": payload.get("stats", {}).get("nodes", 0),
            }
        )
    return entries


def delete_trace(address: str) -> bool:
    path = _path_for(address)
    if path.exists():
        path.unlink()
        return True
    return False
