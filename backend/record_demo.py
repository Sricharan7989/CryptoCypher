"""
Record a trace to data/cache/ so the demo replays instantly.

Run this once, on a good connection, before the presentation:

    python record_demo.py 0x62425cd6bdcb6bfe51558ea465b063486b70dc9f --depth 3

Afterwards /trace and /report serve that address from disk in milliseconds,
with no network call at all. Any address that has NOT been recorded still runs
live, so a fresh trace on a second address remains part of the demo.

    python record_demo.py --list      show what is recorded
    python record_demo.py --clear ADDR  remove one recording
"""

import argparse
import asyncio
import sys

import replay
import tracer
from etherscan import is_valid_address


async def record(address: str, depth: int, dust: float) -> int:
    if not is_valid_address(address):
        print(f"error: '{address}' is not a valid Ethereum address", file=sys.stderr)
        return 2

    print(f"Tracing {address} live at depth {depth} — this may take a minute…")
    result = await tracer.trace(address, max_depth=depth, dust_threshold=dust)
    payload = tracer.to_json(result)
    payload["source"] = "live"

    path = replay.save_trace(payload)
    summary = payload["summary"]
    stats = payload["stats"]

    print()
    print(f"  {summary['headline']}")
    if summary.get("confidence_breakdown"):
        print(f"  why: {summary['confidence_breakdown']}")
    print(f"  {stats['nodes']} wallets, {stats['edges']} transfers, "
          f"{stats['api_calls']} API calls, {stats['elapsed_sec']}s")
    print(f"  risk flags: {len(payload.get('risk_flags', []))}")
    print()
    print(f"Recorded to {path}")
    print("This address will now replay instantly.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a trace for instant demo replay.")
    parser.add_argument("address", nargs="?", help="Ethereum address to record")
    parser.add_argument("--depth", type=int, default=3, help="hops to follow (default 3)")
    parser.add_argument("--dust", type=float, default=0.001, help="dust threshold in ETH")
    parser.add_argument("--list", action="store_true", help="list recorded traces")
    parser.add_argument("--clear", metavar="ADDR", help="delete one recording")
    args = parser.parse_args()

    if args.list:
        entries = replay.list_cached()
        if not entries:
            print("No recorded traces yet.")
            return 0
        print(f"{len(entries)} recorded trace(s):\n")
        for entry in entries:
            print(f"  {entry['address']}")
            print(f"    {entry['headline']}")
            print(f"    recorded {entry['recorded_at']} · {entry['nodes']} wallets\n")
        return 0

    if args.clear:
        print("Removed." if replay.delete_trace(args.clear) else "Nothing recorded for that address.")
        return 0

    if not args.address:
        parser.print_help()
        return 1

    return asyncio.run(record(args.address, args.depth, args.dust))


if __name__ == "__main__":
    sys.exit(main())
