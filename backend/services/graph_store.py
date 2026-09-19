"""
The money-flow graph store — Neo4j when it is available, NetworkX when it is not.

WHY TWO BACKENDS
----------------
Neo4j is the right long-term home for this data. A money trail IS a graph, and a
graph database gives us three things the in-memory engine cannot: the graph
survives the request, several cases can be queried together ("which other
investigations touched this wallet?"), and traversal is expressed in Cypher
rather than reimplemented in Python.

But a database is a server, and a server can be down. This tool exists to be
used in front of people - police, judges, an evaluation panel - and a demo that
dies because a container did not start is a worse outcome than a demo that runs
without its database. So Neo4j is the preferred backend, never a required one.
If it cannot be reached the tracer uses NetworkX and returns an identical
answer; the only thing lost is persistence.

Both backends implement the same narrow interface below. That interface is
deliberately small - it is exactly the set of operations the tracer and the
identification module actually perform on a graph, and nothing more - because
every method here has to be written twice and proven to agree.

THE NEO4J MODEL
---------------
    (:Wallet {trace_id, address, depth, is_start, label, entity_type, ...})
        -[:SENT {trace_id, value_eth, tx_count, timestamp, tx_hash, depth}]->
    (:Wallet {...})

Everything is scoped by `trace_id` so concurrent or historical traces never
bleed into one another. Cross-case analysis is still one query away, because the
same wallet address appears under every trace_id that touched it:

    MATCH (w:Wallet {address: $a}) RETURN DISTINCT w.trace_id
"""

from __future__ import annotations

import logging
import uuid

import networkx as nx

from app import config

log = logging.getLogger(__name__)

# Cap on the relationship depth Cypher will search when finding a path. Must be
# at least the maximum trace depth; a bounded pattern keeps a pathological graph
# from turning one query into a full traversal.
MAX_PATH_HOPS = 12


class MemoryStore:
    """
    NetworkX-backed store. This is the behaviour the tool has always had.

    It stays the fallback rather than being retired because it has no failure
    mode: no connection, no auth, no container. For a graph of a few hundred
    nodes that lives for one request, it is also simply faster than a database.
    """

    backend = "memory"

    def __init__(self, trace_id: str | None = None) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex
        self._g = nx.DiGraph()

    # --- writes ---------------------------------------------------------
    def add_wallet(self, address: str, **attrs) -> None:
        if address in self._g:
            self._g.nodes[address].update(attrs)
        else:
            self._g.add_node(address, **attrs)

    def update_wallet(self, address: str, **attrs) -> None:
        if address in self._g:
            self._g.nodes[address].update(attrs)

    def add_transfer(self, src: str, dst: str, **attrs) -> None:
        self._g.add_edge(src, dst, **attrs)

    def flush(self) -> None:
        """No-op: in-memory writes are immediately visible."""

    # --- reads ----------------------------------------------------------
    def has(self, address: str) -> bool:
        return address in self._g

    def wallet(self, address: str) -> dict:
        return dict(self._g.nodes[address]) if address in self._g else {}

    def wallet_count(self) -> int:
        return self._g.number_of_nodes()

    def transfer_count(self) -> int:
        return self._g.number_of_edges()

    def wallets(self) -> list[tuple[str, dict]]:
        return [(a, dict(d)) for a, d in self._g.nodes(data=True)]

    def transfers(self) -> list[tuple[str, str, dict]]:
        return [(s, t, dict(d)) for s, t, d in self._g.edges(data=True)]

    def predecessors(self, address: str) -> set[str]:
        if address not in self._g:
            return set()
        return {p for p in self._g.predecessors(address) if p != address}

    def in_degrees(self) -> list[int]:
        return [d for _, d in self._g.in_degree()]

    def incoming(self, address: str) -> list[dict]:
        if address not in self._g:
            return []
        return [dict(d) for _, _, d in self._g.in_edges(address, data=True)]

    def shortest_path(self, start: str, target: str) -> list[str]:
        if start == target:
            return [start]
        try:
            return nx.shortest_path(self._g, start, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

    def to_networkx(self) -> nx.DiGraph:
        return self._g

    def close(self) -> None:
        pass


class Neo4jStore:
    """
    Cypher-backed store. Writes are buffered and flushed before every read.

    WHY BUFFERED: a forward trace writes in bursts - one wallet, then up to 25
    outgoing transfers - and sending each of those as its own round trip would
    add more latency than the Etherscan call that produced them. Buffering turns
    a wallet's whole expansion into a single UNWIND. Every read flushes first,
    so a caller can never observe a stale graph; the batching is invisible.
    """

    backend = "neo4j"

    def __init__(self, driver, trace_id: str | None = None) -> None:
        self._driver = driver
        self.trace_id = trace_id or uuid.uuid4().hex
        self._pending_wallets: dict[str, dict] = {}
        self._pending_transfers: list[dict] = []
        self._ensure_schema()

    def _run(self, cypher: str, **params):
        with self._driver.session() as session:
            return list(session.run(cypher, trace_id=self.trace_id, **params))

    def _ensure_schema(self) -> None:
        # An index, not a constraint: composite uniqueness is not available on
        # every Neo4j edition, and MERGE already gives us the uniqueness we need.
        self._run(
            "CREATE INDEX wallet_trace_address IF NOT EXISTS "
            "FOR (w:Wallet) ON (w.trace_id, w.address)"
        )

    # --- writes ---------------------------------------------------------
    def add_wallet(self, address: str, **attrs) -> None:
        self._pending_wallets.setdefault(address, {}).update(attrs)

    def update_wallet(self, address: str, **attrs) -> None:
        self.add_wallet(address, **attrs)

    def add_transfer(self, src: str, dst: str, **attrs) -> None:
        self._pending_transfers.append({"src": src, "dst": dst, **attrs})

    def flush(self) -> None:
        if self._pending_wallets:
            rows = [{"address": a, "props": p} for a, p in self._pending_wallets.items()]
            self._run(
                """
                UNWIND $rows AS row
                MERGE (w:Wallet {trace_id: $trace_id, address: row.address})
                SET w += row.props
                """,
                rows=rows,
            )
            self._pending_wallets.clear()

        if self._pending_transfers:
            self._run(
                """
                UNWIND $rows AS row
                MERGE (a:Wallet {trace_id: $trace_id, address: row.src})
                MERGE (b:Wallet {trace_id: $trace_id, address: row.dst})
                MERGE (a)-[r:SENT {trace_id: $trace_id}]->(b)
                SET r.value_eth = row.value_eth,
                    r.tx_count  = row.tx_count,
                    r.timestamp = row.timestamp,
                    r.tx_hash   = row.tx_hash,
                    r.depth     = row.depth
                """,
                rows=self._pending_transfers,
            )
            self._pending_transfers.clear()

    # --- reads ----------------------------------------------------------
    def has(self, address: str) -> bool:
        # Answer from the buffer when possible - the tracer asks this constantly.
        if address in self._pending_wallets:
            return True
        self.flush()
        rows = self._run(
            "MATCH (w:Wallet {trace_id: $trace_id, address: $address}) RETURN count(w) AS n",
            address=address,
        )
        return bool(rows and rows[0]["n"])

    def wallet(self, address: str) -> dict:
        self.flush()
        rows = self._run(
            "MATCH (w:Wallet {trace_id: $trace_id, address: $address}) RETURN properties(w) AS p",
            address=address,
        )
        if not rows:
            return {}
        props = dict(rows[0]["p"])
        props.pop("trace_id", None)
        props.pop("address", None)
        return props

    def wallet_count(self) -> int:
        self.flush()
        rows = self._run("MATCH (w:Wallet {trace_id: $trace_id}) RETURN count(w) AS n")
        return rows[0]["n"] if rows else 0

    def transfer_count(self) -> int:
        self.flush()
        rows = self._run(
            "MATCH (:Wallet {trace_id: $trace_id})-[r:SENT {trace_id: $trace_id}]->() "
            "RETURN count(r) AS n"
        )
        return rows[0]["n"] if rows else 0

    def wallets(self) -> list[tuple[str, dict]]:
        self.flush()
        out = []
        for row in self._run(
            "MATCH (w:Wallet {trace_id: $trace_id}) RETURN w.address AS a, properties(w) AS p"
        ):
            props = dict(row["p"])
            props.pop("trace_id", None)
            props.pop("address", None)
            out.append((row["a"], props))
        return out

    def transfers(self) -> list[tuple[str, str, dict]]:
        self.flush()
        out = []
        for row in self._run(
            """
            MATCH (a:Wallet {trace_id: $trace_id})-[r:SENT {trace_id: $trace_id}]->(b:Wallet)
            RETURN a.address AS src, b.address AS dst, properties(r) AS p
            """
        ):
            props = dict(row["p"])
            props.pop("trace_id", None)
            out.append((row["src"], row["dst"], props))
        return out

    def predecessors(self, address: str) -> set[str]:
        self.flush()
        rows = self._run(
            """
            MATCH (p:Wallet)-[:SENT {trace_id: $trace_id}]->(w:Wallet {trace_id: $trace_id, address: $address})
            WHERE p.address <> $address
            RETURN DISTINCT p.address AS a
            """,
            address=address,
        )
        return {row["a"] for row in rows}

    def in_degrees(self) -> list[int]:
        """Distinct-sender counts for every wallet, for the adaptive fan-in floor."""
        self.flush()
        rows = self._run(
            """
            MATCH (w:Wallet {trace_id: $trace_id})
            OPTIONAL MATCH (p:Wallet)-[:SENT {trace_id: $trace_id}]->(w)
            WHERE p.address <> w.address
            RETURN count(DISTINCT p) AS indeg
            """
        )
        return [row["indeg"] for row in rows]

    def incoming(self, address: str) -> list[dict]:
        self.flush()
        rows = self._run(
            """
            MATCH (:Wallet)-[r:SENT {trace_id: $trace_id}]->(w:Wallet {trace_id: $trace_id, address: $address})
            RETURN properties(r) AS p
            """,
            address=address,
        )
        return [dict(row["p"]) for row in rows]

    def shortest_path(self, start: str, target: str) -> list[str]:
        """
        Fewest-hops route, computed by Cypher's shortestPath.

        The hop bound is required, not cosmetic: an unbounded variable-length
        pattern on a graph with cycles is how a query becomes a full traversal.
        """
        if start == target:
            return [start]
        self.flush()
        rows = self._run(
            f"""
            MATCH (a:Wallet {{trace_id: $trace_id, address: $start}}),
                  (b:Wallet {{trace_id: $trace_id, address: $target}})
            MATCH path = shortestPath((a)-[:SENT*..{MAX_PATH_HOPS}]->(b))
            RETURN [n IN nodes(path) | n.address] AS addresses
            """,
            start=start,
            target=target,
        )
        return list(rows[0]["addresses"]) if rows else []

    def to_networkx(self) -> nx.DiGraph:
        """
        Materialise a NetworkX view of the finished graph.

        Used only for serialisation and for the parts of the codebase that
        already speak DiGraph. A few hundred nodes, read once at the end of a
        trace - so this costs one query, not a second traversal engine.
        """
        graph = nx.DiGraph()
        for address, props in self.wallets():
            graph.add_node(address, **props)
        for src, dst, props in self.transfers():
            graph.add_edge(src, dst, **props)
        return graph

    def close(self) -> None:
        try:
            self.flush()
        except Exception:  # noqa: BLE001 - never let cleanup mask a real error
            log.warning("Neo4j flush failed during close", exc_info=True)


# --- selection ----------------------------------------------------------------

_driver = None
_driver_checked = False


def _get_driver():
    """
    Connect to Neo4j once per process, and remember failure.

    Remembering the failure matters: without it, every trace would pay the
    connection timeout again while the database stays down.
    """
    global _driver, _driver_checked

    if _driver_checked:
        return _driver
    _driver_checked = True

    if config.NEO4J_DISABLED:
        log.info("Neo4j disabled by NEO4J_DISABLED; using in-memory graph")
        return None

    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
            connection_timeout=config.NEO4J_CONNECT_TIMEOUT,
        )
        driver.verify_connectivity()
        _driver = driver
        log.info("Connected to Neo4j at %s", config.NEO4J_URI)
    except Exception as exc:  # noqa: BLE001 - any failure means fall back
        log.info("Neo4j unavailable (%s); using in-memory graph", exc)
        _driver = None

    return _driver


def get_store(trace_id: str | None = None, prefer: str | None = None):
    """
    A graph store for one trace: Neo4j if reachable, otherwise in-memory.

    `prefer="memory"` forces the fallback - used by the tests, and worth knowing
    for a demo, where `NEO4J_DISABLED=1` proves the tool still works with the
    database switched off.
    """
    if prefer == "memory":
        return MemoryStore(trace_id)

    driver = _get_driver()
    if driver is None:
        return MemoryStore(trace_id)

    try:
        return Neo4jStore(driver, trace_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Neo4j store unusable (%s); using in-memory graph", exc)
        return MemoryStore(trace_id)


def reset_driver() -> None:
    """Drop the cached driver so the next call re-probes. For tests."""
    global _driver, _driver_checked
    if _driver is not None:
        try:
            _driver.close()
        except Exception:  # noqa: BLE001
            pass
    _driver = None
    _driver_checked = False


def status() -> dict:
    """What the /health endpoint reports about the graph backend."""
    driver = _get_driver()
    return {
        "backend": "neo4j" if driver is not None else "memory",
        "uri": config.NEO4J_URI if driver is not None else None,
        "disabled": config.NEO4J_DISABLED,
    }
