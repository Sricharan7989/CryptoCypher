"""
The tracing engine — follows stolen funds forward until they reach a VASP.

THE CORE INSIGHT THIS MODULE IMPLEMENTS
---------------------------------------
Criminals launder crypto by moving it through a chain of fresh, anonymous,
self-controlled "unhosted" wallets. Those wallets are brand new and carry no
identity, so trying to recognise THEM is hopeless. But crypto only becomes
spendable cash at a centralised exchange (a VASP), which performed KYC and
cannot hide: it is a large regulated business with a stable, recognisable
on-chain fingerprint.

So we do not try to identify the criminal. We follow the money THROUGH the
anonymous wallets until it arrives somewhere we recognise. This module walks
that trail and calls identify.py at every wallet it meets; the moment a wallet
is recognised as an exchange, that branch is finished - we have found the exit
point, and that is the actionable answer.

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

Four limits keep this bounded, and each discards the LEAST informative work:
  * identification  - stop the moment a branch reaches an exchange, mixer or
                      bridge. This is the most valuable cap of the four: it
                      ends branches exactly where the answer is, and it stops
                      us from expanding the highest-degree wallets on the whole
                      chain, which is what exchange hot wallets are.
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
stablecoin, so this is the first gap to close.
"""

import time
from collections import deque
from dataclasses import dataclass, field

import networkx as nx

import config
import graph_store
import identify
import scoring
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
class Attribution:
    """
    A recognised endpoint: the answer the investigation is looking for.

    `hop_distance` is the headline number - "funds reached Binance 3 hops away".
    Because the walk is breadth-first, it is the SHORTEST path to that entity,
    not an artefact of traversal order. `method` and `confidence` travel with it
    so the claim can be weighed rather than taken on faith.
    """

    address: str
    entity: str
    entity_type: str  # exchange | suspected_exchange | mixer | bridge
    method: str
    confidence: float
    hop_distance: int
    evidence: str
    value_received_eth: float = 0.0

    # The shortest route from the suspect wallet to this address, and the label
    # types crossed along it. Both are filled in after the walk, because a path
    # is only knowable once the graph is complete. scoring.compute_confidence
    # reads `path_risk_types` to apply the mixer and bridge penalties.
    path: list[str] = field(default_factory=list)
    path_risk_types: set[str] = field(default_factory=set)
    confidence_score: int = 0  # 0-100, from scoring.py
    confidence_breakdown: str = ""
    confidence_components: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "entity": self.entity,
            "entity_type": self.entity_type,
            "method": self.method,
            "confidence": round(self.confidence, 2),
            "confidence_score": self.confidence_score,
            "confidence_breakdown": self.confidence_breakdown,
            "confidence_components": self.confidence_components,
            "hop_distance": self.hop_distance,
            "evidence": self.evidence,
            "value_received_eth": round(self.value_received_eth, 6),
            "path": self.path,
            "crossed": sorted(self.path_risk_types),
        }


@dataclass
class RiskFlag:
    """
    A wallet on the money trail that carries a risk of its own.

    Distinct from an Attribution: an attribution answers "whose wallet is this",
    a risk flag answers "what is wrong with this wallet being on the path". A
    mixer is both - the exchange search stops there, AND it is a red flag.
    """

    address: str
    entity: str
    risk_type: str  # mixer | bridge | scam | sanctioned
    severity: str  # critical | high | medium
    hop_distance: int
    on_primary_path: bool
    value_received_eth: float
    note: str

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "entity": self.entity,
            "risk_type": self.risk_type,
            "severity": self.severity,
            "hop_distance": self.hop_distance,
            "on_primary_path": self.on_primary_path,
            "value_received_eth": round(self.value_received_eth, 6),
            "note": self.note,
        }


@dataclass
class TraceResult:
    """
    What a trace produced: the money-flow graph, the hops, and the attributions.

    Unpacks like a tuple (`graph, hops = await trace(addr)`) and also carries the
    run statistics the UI shows and that tell us whether the caps were hit.
    """

    graph: nx.DiGraph
    hops: list[Hop]
    start_address: str
    max_depth: int
    dust_threshold: float
    attributions: list[Attribution] = field(default_factory=list)
    risk_flags: list[RiskFlag] = field(default_factory=list)
    truncated: bool = False  # a cap stopped the walk early
    notes: list[str] = field(default_factory=list)
    api_calls: int = 0
    cache_hits: int = 0
    elapsed_sec: float = 0.0
    backend: str = "memory"  # which graph store served this trace

    def __iter__(self):
        """So `graph, hops = result` works, as the engine's contract promises."""
        return iter((self.graph, self.hops))

    @property
    def exchanges(self) -> list[Attribution]:
        """Attributions that are actually actionable - a VASP that can be served."""
        return [
            a for a in self.attributions
            if a.entity_type in ("exchange", "suspected_exchange")
        ]

    @property
    def flags(self) -> list[Attribution]:
        """Obfuscation encountered on the way: mixers and cross-chain bridges."""
        return [a for a in self.attributions if a.entity_type in ("mixer", "bridge")]


def _mark_node(store, address: str, ident: identify.Identification) -> None:
    """Write an identification onto the wallet so the frontend can style and label it."""
    store.update_wallet(
        address,
        label=ident.entity,
        entity_type=ident.entity_type,
        method=ident.method,
        confidence=ident.confidence,
        is_vasp=ident.entity_type in ("exchange", "suspected_exchange"),
        is_mixer=ident.entity_type == "mixer",
        is_bridge=ident.entity_type == "bridge",
    )


def _value_received(store, address: str) -> float:
    """Total ETH that reached this address along traced edges."""
    return sum(edge.get("value_eth", 0.0) for edge in store.incoming(address))


async def trace(
    start_address: str,
    max_depth: int = 4,
    dust_threshold: float = 0.001,
    client: EtherscanClient | None = None,
) -> TraceResult:
    """
    Walk the money forward from `start_address` until it reaches a known entity.

    Breadth-first, not depth-first, and that choice is deliberate: BFS visits
    wallets in order of hop distance, so the first time we reach any wallet we
    have reached it by the SHORTEST path. When the result says "the funds
    reached Binance 3 hops away", BFS is what makes that number true rather than
    an artefact of traversal order.

    Identification runs at two moments, for a reason. A label lookup happens the
    instant a wallet is discovered, since it depends only on the address. The
    consolidation heuristic is re-checked when a wallet is about to be expanded,
    and once more after the walk finishes, because fan-in is a property of the
    graph and the graph is still growing - a wallet that looks ordinary when
    first seen may turn out to be where six separate branches converge.

    Args:
        start_address: the suspect wallet the investigator was given.
        max_depth: how many hops forward to follow. See the fan-out note above.
        dust_threshold: ignore transfers below this many ETH.
        client: injectable Etherscan client, for tests and replay mode.

    Returns:
        TraceResult - `.graph`, `.hops`, and `.attributions` (recognised
        endpoints, nearest first). Confidence is never 1.0.

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

    store = graph_store.get_store()
    hops: list[Hop] = []
    notes: list[str] = []
    attributions: dict[str, Attribution] = {}  # keyed by address, first hit wins
    truncated = False

    def record(address: str, ident: identify.Identification, depth: int) -> None:
        """Mark the wallet and log the attribution, keeping the shortest hop distance."""
        _mark_node(store, address, ident)
        if address in attributions:
            return
        attributions[address] = Attribution(
            address=address,
            entity=ident.entity,
            entity_type=ident.entity_type,
            method=ident.method,
            confidence=ident.confidence,
            hop_distance=depth,
            evidence=ident.evidence,
        )

    # depth = hops from the start address. The suspect wallet itself is depth 0.
    store.add_wallet(start, depth=0, is_start=True)

    # If the suspect address is ITSELF a known entity, that is worth reporting -
    # but we still expand it. Stopping at depth 0 would return an empty graph
    # and tell the investigator nothing about where the money went.
    start_ident = identify.known_label_lookup(start)
    if start_ident is not None:
        record(start, start_ident, 0)
        notes.append(
            f"The start address is itself a known entity ({start_ident.entity}); "
            f"tracing onward from it anyway."
        )

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
        if store.wallet_count() >= config.MAX_NODES_PER_TRACE:
            truncated = True
            notes.append(
                f"Stopped expanding at {config.MAX_NODES_PER_TRACE} wallets "
                f"(MAX_NODES_PER_TRACE). The graph is partial."
            )
            break

        # Re-check before spending an API call. Fan-in may have matured since
        # this wallet was queued, and if it is now recognisable the branch is
        # already answered - expanding an exchange hot wallet would be both
        # pointless and ruinously expensive.
        if depth > 0:
            ident = identify.identify(address, store)
            if ident is not None:
                record(address, ident, depth)
                if identify.is_terminal(ident):
                    continue

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

            if not store.has(to_addr):
                store.add_wallet(to_addr, depth=child_depth, is_start=False)

            store.add_transfer(
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

            # Label lookup the moment the wallet is discovered - it needs only
            # the address, so there is no reason to wait.
            child_ident = identify.known_label_lookup(to_addr)
            if child_ident is not None:
                record(to_addr, child_ident, child_depth)
                if identify.is_terminal(child_ident):
                    # Branch complete: we found where this money came to rest.
                    continue

            if to_addr not in expanded and child_depth < max_depth:
                queue.append((to_addr, child_depth))

    # Final consolidation sweep. Fan-in is only fully known once the walk is
    # over, so a wallet where several branches converged may become recognisable
    # here even though it looked ordinary every time we saw it mid-walk.
    for address, data in store.wallets():
        if address in attributions or address == start:
            continue
        late = identify.consolidation_identify(address, store)
        if late is not None:
            record(address, late, data.get("depth", 0))
            notes.append(
                f"{address} was identified as a consolidation point only after "
                f"the walk completed; its onward transfers were still followed."
            )

    # Attach how much value actually reached each identified endpoint.
    for attribution in attributions.values():
        attribution.value_received_eth = _value_received(store, attribution.address)

    # Reconstruct each attribution's route and score it. This has to happen
    # after the walk: a path is only knowable once the graph is complete, and
    # the score depends on what that path crossed.
    for attribution in attributions.values():
        attribution.path = store.shortest_path(start, attribution.address)
        attribution.path_risk_types = _risk_types_on_path(store, attribution.path)

        scored = scoring.compute_confidence(attribution)
        attribution.confidence_score = scored.score
        attribution.confidence_breakdown = scored.breakdown
        attribution.confidence_components = [c.to_dict() for c in scored.components]
        # Keep the 0-1 field in step so every existing consumer stays correct.
        attribution.confidence = scored.score / 100.0

    # Nearest first, then most confident: the closest exit point is the one an
    # investigator should act on.
    ordered = sorted(
        attributions.values(), key=lambda a: (a.hop_distance, -a.confidence)
    )

    risk_flags = _collect_risk_flags(store, start, ordered)

    # Materialise a NetworkX view for serialisation and for everything that
    # already speaks DiGraph. With Neo4j this is one query at the end of the
    # walk, not a second traversal engine running alongside the first.
    graph = store.to_networkx()
    backend = store.backend
    store.close()

    return TraceResult(
        graph=graph,
        hops=hops,
        start_address=start,
        max_depth=max_depth,
        dust_threshold=dust_threshold,
        attributions=ordered,
        risk_flags=risk_flags,
        truncated=truncated,
        notes=notes,
        api_calls=client.api_calls,
        cache_hits=client.cache_hits,
        elapsed_sec=round(time.monotonic() - started_at, 2),
        backend=backend,
    )


def _risk_types_on_path(store, path: list[str]) -> set[str]:
    """
    Which risky label types the money crossed on the way to the endpoint.

    The endpoint itself is excluded: we are scoring how trustworthy the route TO
    it is, and a mixer should not be penalised for being a mixer. Its own
    intermediate hops still count.
    """
    crossed: set[str] = set()
    for address in path[:-1]:
        entity_type = store.wallet(address).get("entity_type")
        if scoring.risk_severity(entity_type or "") is not None:
            crossed.add(entity_type)
    return crossed


def _primary_attribution(attributions: list[Attribution]) -> Attribution | None:
    """
    The one finding the report is actually about - the headline.

    NOT simply the first attribution. Attributions are ordered by hop distance,
    so a mixer one hop out sorts ahead of the exchange three hops out, but the
    exchange is the finding; the mixer is something the money passed on the way.
    Picking the wrong one here would make "on this path" contradict the path the
    panel draws, so this deliberately mirrors what summarize() reports: a named
    exchange first, an unconfirmed collection point only if there is no named
    one, and nothing at all if neither exists.
    """
    for wanted in ("exchange", "suspected_exchange"):
        for attribution in attributions:
            if attribution.entity_type == wanted:
                return attribution

    # No exchange anywhere: the trail ended at a mixer or a bridge. That IS the
    # story of this trace, so the nearest such endpoint becomes the primary
    # path - otherwise a mixer sitting directly on the money's route would be
    # reported as though it were off to one side.
    return attributions[0] if attributions else None


def _collect_risk_flags(store, start: str, attributions: list[Attribution]) -> list[RiskFlag]:
    """
    Every labelled wallet in the trace that is a risk in its own right.

    Covers mixers, bridges and - when such entries exist in labels.json - scam
    and sanctioned addresses. Each flag records whether it sits on the primary
    path (the route to the headline finding) or elsewhere in the graph, because
    a mixer on the actual trail means something quite different from one on a
    side branch the money never took.
    """
    primary = _primary_attribution(attributions)
    primary_path = set(primary.path) if primary is not None else set()

    flags: list[RiskFlag] = []
    for address, data in store.wallets():
        entity_type = data.get("entity_type")
        severity = scoring.risk_severity(entity_type or "")
        if severity is None:
            continue

        entity = data.get("label") or "Unknown entity"
        flags.append(
            RiskFlag(
                address=address,
                entity=entity,
                risk_type=entity_type,
                severity=severity,
                hop_distance=data.get("depth", 0),
                on_primary_path=address in primary_path,
                value_received_eth=_value_received(store, address),
                note=scoring.risk_note(entity_type, entity),
            )
        )

    # Worst first, and within a severity the ones actually on the trail lead.
    order = {"critical": 0, "high": 1, "medium": 2}
    flags.sort(
        key=lambda f: (
            order.get(f.severity, 9),
            not f.on_primary_path,
            f.hop_distance,
        )
    )
    return flags


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


def summarize(result: TraceResult) -> dict:
    """
    The headline finding, ready for the investigator-facing panel.

    Reports the NEAREST exchange, because hop distance is the strongest
    available proxy for how directly the suspect controlled the deposit. A
    result is always honest about failure: if nothing was recognised, that is
    stated plainly rather than dressed up as a weak hit.
    """
    # A NAMED exchange and a mere consolidation pattern are not interchangeable,
    # and the headline must never blur them. Criminals consolidate too - they
    # re-pool funds from their own split wallets, which produces exactly the
    # fan-in signature an exchange deposit sweep produces. Method (b) cannot
    # tell those apart, so a suspected hit is only ever reported as a lead.
    confirmed = [a for a in result.exchanges if a.entity_type == "exchange"]
    suspected = [a for a in result.exchanges if a.entity_type == "suspected_exchange"]
    exchanges = confirmed
    flags = result.flags

    if not confirmed and suspected:
        lead = suspected[0]
        return {
            "found": False,
            "lead": True,
            "headline": (
                f"No named exchange reached within {result.max_depth} hops. "
                f"One collection point found {lead.hop_distance} hops away "
                f"({lead.confidence_score}% confidence) - UNCONFIRMED."
            ),
            "address": lead.address,
            "hop_distance": lead.hop_distance,
            "confidence": round(lead.confidence, 2),
            "confidence_score": lead.confidence_score,
            "confidence_breakdown": lead.confidence_breakdown,
            "confidence_components": lead.confidence_components,
            "method": lead.method,
            "value_received_eth": round(lead.value_received_eth, 6),
            "recommended_action": (
                f"Do NOT treat {lead.address} as an exchange yet. Many wallets "
                f"funnel into it, but a criminal re-pooling their own split "
                f"funds produces the same pattern. Verify it independently "
                f"(Etherscan labels, outgoing transaction count) before any "
                f"lawful request is raised."
            ),
            "mixers_or_bridges_crossed": [f.entity for f in flags],
        }

    if not exchanges:
        return {
            "found": False,
            "lead": False,
            "headline": (
                "No known exchange reached within "
                f"{result.max_depth} hops of {result.start_address[:10]}..."
            ),
            "recommended_action": (
                "Widen the trace depth, or expand labels.json. Funds may still "
                "be sitting in unhosted wallets or have moved via ERC-20 "
                "tokens, which this build does not yet follow."
            ),
            "mixers_or_bridges_crossed": [f.entity for f in flags],
        }

    nearest = exchanges[0]
    return {
        "found": True,
        "lead": False,
        "exchange": nearest.entity,
        "address": nearest.address,
        "hop_distance": nearest.hop_distance,
        "confidence": round(nearest.confidence, 2),
        "confidence_score": nearest.confidence_score,
        "confidence_breakdown": nearest.confidence_breakdown,
        "confidence_components": nearest.confidence_components,
        "method": nearest.method,
        "value_received_eth": round(nearest.value_received_eth, 6),
        "headline": (
            f"Funds reached {nearest.entity}, {nearest.hop_distance} "
            f"hop{'s' if nearest.hop_distance != 1 else ''} away, "
            f"{nearest.confidence_score}% confidence"
        ),
        "recommended_action": (
            f"Serve a lawful data request to {nearest.entity} via SAHYOG for "
            f"KYC records on deposits to {nearest.address}."
        ),
        "mixers_or_bridges_crossed": [f.entity for f in flags],
        "other_exchanges_reached": [a.entity for a in exchanges[1:]],
        "unconfirmed_collection_points": [a.address for a in suspected],
    }


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
            "label": data.get("label"),
            "entity_type": data.get("entity_type"),
            "is_vasp": data.get("is_vasp", False),
            "is_mixer": data.get("is_mixer", False),
            "is_bridge": data.get("is_bridge", False),
            "confidence": data.get("confidence"),
            "method": data.get("method"),
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
        "summary": summarize(result),
        "attributions": [a.to_dict() for a in result.attributions],
        "exchanges": [a.to_dict() for a in result.exchanges],
        "flags": [a.to_dict() for a in result.flags],
        "risk_flags": [f.to_dict() for f in result.risk_flags],
        "stats": {
            "nodes": result.graph.number_of_nodes(),
            "edges": result.graph.number_of_edges(),
            "hops": len(result.hops),
            "max_depth_reached": max((n["depth"] for n in nodes), default=0),
            "identified": len(result.attributions),
            "labels_loaded": identify.label_count(),
            "api_calls": result.api_calls,
            "cache_hits": result.cache_hits,
            "elapsed_sec": result.elapsed_sec,
            "truncated": result.truncated,
            "graph_backend": result.backend,
        },
        "notes": result.notes,
        "nodes": nodes,
        "edges": edges,
        "hops": [hop.to_dict() for hop in result.hops],
    }
