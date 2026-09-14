"""
The tracing engine — follows stolen funds forward, hop by hop, across Ethereum.

THE CORE INSIGHT THIS MODULE IMPLEMENTS
---------------------------------------
Criminals launder crypto by moving it through a chain of fresh, anonymous,
self-controlled "unhosted" wallets. Those wallets are brand new and carry no
identity, so trying to recognise THEM is hopeless. But crypto only becomes
spendable cash at a centralised exchange (a VASP), which performed KYC and
cannot hide: it is a large regulated business with a stable, recognisable
on-chain fingerprint.

So we do not try to identify the criminal. We follow the money THROUGH the
anonymous wallets until it arrives somewhere we recognise. This module builds
that trail. Naming the exchange at the end of it is a later step - the tracer
deliberately stops at "here is the money flow" and makes no attribution claim.

WHY WE FOLLOW OUTGOING EDGES
----------------------------
At every wallet we ask "where did the money go NEXT", never "where did it come
from". An edge is only added for a transaction where the current node is the
SENDER. Following incoming edges instead would walk backwards into the funds'
history - the victims, the earlier owners - which is the opposite of what an
investigator needs. They already know the suspect; what they need is the
downstream exit point where the funds hit a KYC'd business that can freeze them
and identify the account holder. The direction of traversal IS the product.

WHY WE CAP DEPTH (fan-out explosion)
------------------------------------
Ethereum wallets have unbounded out-degree. If each wallet sends to just 20
others, an uncapped forward walk touches 20 wallets at hop 1, 400 at hop 2,
8,000 at hop 3 and 160,000 at hop 4 - and a single exchange hot wallet alone can
send to tens of thousands of addresses, so in practice it is far worse than
that. Each wallet also costs an API call against a 5-calls/sec free tier, so an
uncapped trace does not merely get slow, it never finishes and gets the API key
rate-limited on the way.

Three limits keep this bounded, and each discards the LEAST informative work:
  * max_depth       - laundering chains are short in practice; the exit point is
                      usually within a few hops, and everything past that is
                      noise anyway.
  * dust_threshold  - tiny transfers are spam, gas dust and airdrop noise. The
                      stolen principal moves in meaningful amounts, so following
                      value below the threshold chases decoys, not the money.
  * MAX_EDGES_PER_NODE - per wallet, expand only the largest outgoing transfers.
                      If a wallet split funds 500 ways, the big branches are
                      where the principal went; the long tail is chaff.

SCOPE (current stage)
---------------------
Native ETH transfers only. ERC-20 movements (USDT, USDC) and internal contract
transfers are NOT traced yet - a real laundering path often converts to a
stablecoin, so this is the first gap to close after the trace works end to end.
"""

import time
from collections import deque
from dataclasses import dataclass, field

import networkx as nx

import config
from etherscan import EtherscanClient, Transfer, get_client, normalize_address


@dataclass
class Hop:
    """
    One step of the money flow: funds moving from one wallet to the next.

    A Hop is an aggregate, not a single transaction. If a wallet paid the same
    recipient eleven times, that is one hop carrying the summed value - an
    investigator cares that the money went A -> B and how much, not that it was
    split across eleven transactions.
    """

    depth: int  # hops from the start address; the first hop out is depth 1
    from_addr: str
    to_addr: str
    value_eth: float
    tx_count: int
    timestamp: int  # most recent transaction on this edge, unix epoch
    tx_hash: str  # the single largest transaction, as a citable example

    def to_dict(self) -> dict:
        return {
            "depth": self.depth,
            "from": self.from_addr,
            "to": self.to_addr,
            "value_eth": round(self.value_eth, 6),
            "tx_count": self.tx_count,
            "timestamp": self.timestamp,
            "tx_hash": self.tx_hash,
        }


@dataclass
class TraceResult:
    """
    What a trace produced: the money-flow graph, plus the hops in the order walked.

    Unpacks like a tuple (`graph, hops = await trace(addr)`) and also carries the
    run statistics the UI shows and that tell us whether the caps were hit.
    """

    graph: nx.DiGraph
    hops: list[Hop]
    start_address: str
    max_depth: int
    dust_threshold: float
    truncated: bool = False  # a cap stopped the walk early
    notes: list[str] = field(default_factory=list)
    api_calls: int = 0
    cache_hits: int = 0
    elapsed_sec: float = 0.0

    def __iter__(self):
        """So `graph, hops = result` works, as the engine's contract promises."""
        return iter((self.graph, self.hops))


async def trace(
    start_address: str,
    max_depth: int = 4,
    dust_threshold: float = 0.001,
    client: EtherscanClient | None = None,
) -> TraceResult:
    """
    Walk the money forward from `start_address` and return the flow graph.

    Breadth-first, not depth-first, and that choice is deliberate: BFS visits
    wallets in order of hop distance, so the first time we reach any wallet we
    have reached it by the SHORTEST path. When the exchange-identification step
    later reports "the funds reached Binance 3 hops away", BFS is what makes
    that number true rather than an artefact of traversal order.

    Args:
        start_address: the suspect wallet the investigator was given.
        max_depth: how many hops forward to follow. See the fan-out note above.
        dust_threshold: ignore transfers below this many ETH.
        client: injectable Etherscan client, for tests and replay mode.

    Returns:
        TraceResult - `.graph` (nodes = wallets, edges = aggregated flows) and
        `.hops` (every edge in BFS order). Makes NO claim about who owns any
        wallet; attribution is a separate, later step.

    Raises:
        ValueError: the start address is not a well-formed Ethereum address.
        EtherscanError: the very first fetch failed, so there is no trace at all.
    """
    started_at = time.monotonic()
    client = client or get_client()
    client.reset_stats()

    start = normalize_address(start_address)
    if len(start) != 42 or not start.startswith("0x"):
        raise ValueError(f"Not a valid Ethereum address: {start_address!r}")

    graph = nx.DiGraph()
    hops: list[Hop] = []
    notes: list[str] = []
    truncated = False

    # depth = hops from the start address. The suspect wallet itself is depth 0.
    graph.add_node(start, depth=0, is_start=True)

    # `expanded` guards against refetching a wallet we have already walked out
    # of. Laundering paths loop and re-converge, so without this the walk can
    # revisit the same wallet along every inbound path - or spin forever on a
    # cycle. The wallet keeps its FIRST (shortest) depth, per the BFS argument.
    expanded: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])

    while queue:
        address, depth = queue.popleft()

        if address in expanded:
            continue
        # Depth cap: this wallet is recorded in the graph, we just do not ask
        # where its money went next. This is the boundary of the trace.
        if depth >= max_depth:
            continue
        if len(graph) >= config.MAX_NODES_PER_TRACE:
            truncated = True
            notes.append(
                f"Stopped expanding at {config.MAX_NODES_PER_TRACE} wallets "
                f"(MAX_NODES_PER_TRACE). The graph is partial."
            )
            break

        expanded.add(address)

        try:
            transfers = await client.get_outgoing_transfers(address)
        except Exception as exc:  # noqa: BLE001 - one bad wallet must not kill the trace
            if address == start:
                # No data for the suspect wallet means there is nothing to show.
                raise
            notes.append(f"Could not fetch {address}: {exc}")
            continue

        # Aggregate first, THEN pick the biggest branches. Ranking raw
        # transactions would let one counterparty paid in 50 small slices lose
        # to a single larger one-off, even though it received far more overall.
        flows = _aggregate_by_recipient(transfers, dust_threshold)
        ranked = sorted(flows.values(), key=lambda f: f["value_eth"], reverse=True)

        if len(ranked) > config.MAX_EDGES_PER_NODE:
            truncated = True
            notes.append(
                f"{address} sent to {len(ranked)} recipients; expanded the "
                f"{config.MAX_EDGES_PER_NODE} largest by value."
            )
            ranked = ranked[: config.MAX_EDGES_PER_NODE]

        child_depth = depth + 1
        for flow in ranked:
            to_addr = flow["to"]

            if to_addr not in graph:
                graph.add_node(to_addr, depth=child_depth, is_start=False)

            graph.add_edge(
                address,
                to_addr,
                value_eth=flow["value_eth"],
                tx_count=flow["tx_count"],
                timestamp=flow["timestamp"],
                tx_hash=flow["tx_hash"],
                depth=child_depth,
            )

            hops.append(
                Hop(
                    depth=child_depth,
                    from_addr=address,
                    to_addr=to_addr,
                    value_eth=flow["value_eth"],
                    tx_count=flow["tx_count"],
                    timestamp=flow["timestamp"],
                    tx_hash=flow["tx_hash"],
                )
            )

            if to_addr not in expanded and child_depth < max_depth:
                queue.append((to_addr, child_depth))

    return TraceResult(
        graph=graph,
        hops=hops,
        start_address=start,
        max_depth=max_depth,
        dust_threshold=dust_threshold,
        truncated=truncated,
        notes=notes,
        api_calls=client.api_calls,
        cache_hits=client.cache_hits,
        elapsed_sec=round(time.monotonic() - started_at, 2),
    )


def _aggregate_by_recipient(transfers: list[Transfer], dust_threshold: float) -> dict:
    """
    Collapse many transactions into one flow per recipient, dropping dust.

    Dust is filtered per TRANSACTION, before summing, on purpose: a thousand
    0.0001 ETH spam sends are still spam even though they total 0.1 ETH, and
    letting them sum past the threshold would reopen the noise the threshold
    exists to shut out.
    """
    flows: dict[str, dict] = {}

    for t in transfers:
        if t.value_eth < dust_threshold:
            continue

        flow = flows.get(t.to_addr)
        if flow is None:
            flows[t.to_addr] = {
                "to": t.to_addr,
                "value_eth": t.value_eth,
                "tx_count": 1,
                "timestamp": t.timestamp,
                "tx_hash": t.hash,
                "_max_value": t.value_eth,
            }
            continue

        flow["value_eth"] += t.value_eth
        flow["tx_count"] += 1
        flow["timestamp"] = max(flow["timestamp"], t.timestamp)
        # Keep the largest single transaction as the edge's citable example -
        # it is the one an investigator would look up on Etherscan first.
        if t.value_eth > flow["_max_value"]:
            flow["_max_value"] = t.value_eth
            flow["tx_hash"] = t.hash

    return flows


def to_json(result: TraceResult) -> dict:
    """
    Flatten a TraceResult into the frontend's JSON shape.

    Deliberately a flat nodes + edges pair using `id` / `source` / `target`,
    which is what Cytoscape.js consumes directly - no reshaping in the browser.
    """
    nodes = [
        {
            "id": address,
            "depth": data.get("depth", 0),
            "is_start": data.get("is_start", False),
            # Populated by the exchange-identification step, not by the tracer.
            "label": None,
            "entity_type": None,
        }
        for address, data in result.graph.nodes(data=True)
    ]
    nodes.sort(key=lambda n: (n["depth"], n["id"]))

    edges = [
        {
            "source": src,
            "target": dst,
            "value_eth": round(data.get("value_eth", 0.0), 6),
            "tx_count": data.get("tx_count", 1),
            "timestamp": data.get("timestamp", 0),
            "tx_hash": data.get("tx_hash", ""),
            "depth": data.get("depth", 0),
        }
        for src, dst, data in result.graph.edges(data=True)
    ]
    edges.sort(key=lambda e: (e["depth"], -e["value_eth"]))

    return {
        "start_address": result.start_address,
        "params": {
            "max_depth": result.max_depth,
            "dust_threshold_eth": result.dust_threshold,
        },
        "stats": {
            "nodes": result.graph.number_of_nodes(),
            "edges": result.graph.number_of_edges(),
            "hops": len(result.hops),
            "max_depth_reached": max(
                (n["depth"] for n in nodes), default=0
            ),
            "api_calls": result.api_calls,
            "cache_hits": result.cache_hits,
            "elapsed_sec": result.elapsed_sec,
            "truncated": result.truncated,
        },
        "notes": result.notes,
        "nodes": nodes,
        "edges": edges,
        "hops": [hop.to_dict() for hop in result.hops],
    }
