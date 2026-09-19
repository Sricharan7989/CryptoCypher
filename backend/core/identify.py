"""
Exchange identification — deciding whether a wallet in the trace is a VASP.

WHY THIS MODULE IS THE PAYOFF
-----------------------------
The tracer maps where money went. This module answers the question that makes
the trace actionable: is this wallet the property of a regulated business that
holds KYC records? A path through twelve anonymous wallets is useless on its
own. The same path ending at a wallet we can name as Binance is a lawful
request away from a human identity.

WHY IDENTIFICATION IS POSSIBLE AT ALL
-------------------------------------
A criminal's wallets are fresh and anonymous, so there is nothing to recognise.
An exchange is the opposite: it serves millions of users, must publish deposit
addresses, and settles enormous volume through a small set of wallets. It
cannot hide, and it cannot cheaply rotate. That asymmetry is the entire basis
of this tool - we never identify the criminal, we identify the exit.

THE FOUR METHODS
----------------
  (a) known_label_lookup   - WORKS. Direct match against data/labels.json.
  (b) consolidation_score  - WORKS. Deposit-consolidation fan-in fingerprint.
  (c) behavioral_classifier - STUB, future scope.
  (d) cospend_cluster      - STUB, not applicable to Ethereum's account model.

Every result carries a confidence below 1.0. This tool informs a lawful
request; it never asserts certainty, and an investigator must be able to see
WHICH method produced a claim in order to weigh it. That is why `method` and
`evidence` travel with every Identification.
"""

import json
from dataclasses import dataclass

import networkx as nx

from app import config
from services import graph_store

# --- Tunables -----------------------------------------------------------------

# Distinct in-graph senders before fan-in is considered meaningful at all.
# Below this, a shared recipient is just as likely to be a merchant, a contract
# or coincidence, so claiming "exchange" would be irresponsible.
CONSOLIDATION_MIN_SENDERS = 4

# Distinct senders at or above which we actually flag the address. Expressed as
# a COUNT rather than as a score cutoff, because the count is the thing with a
# real-world meaning and a reviewer can argue with it: six separate wallets from
# one suspect's flow converging on a single address is a genuine anomaly, since
# a trace only walks a few hundred wallets of one money flow in the first place.
CONSOLIDATION_FLAG_SENDERS = 6

# Fan-in at or above which we treat the consolidation pattern as saturated and
# award the method's maximum confidence.
CONSOLIDATION_STRONG_SENDERS = 12

# Confidence for an exact labels.json hit. Deliberately not 1.0: labels can go
# stale, exchanges rotate wallets, and a published list can simply be wrong.
LABEL_CONFIDENCE = 0.95

# Ceiling for a pure fan-in inference. Far lower than a label match, because
# the pattern is suggestive, not identifying - it says "this behaves like a
# collection point", never "this is Binance".
CONSOLIDATION_MAX_CONFIDENCE = 0.70


@dataclass(frozen=True)
class Identification:
    """
    One attribution claim about one address.

    `method` and `evidence` are not decoration. An investigator acting on this
    has to know whether the name came from a published label or from a
    statistical hunch, because those justify very different actions.
    """

    address: str
    entity: str
    entity_type: str  # exchange | mixer | bridge | suspected_exchange
    method: str  # known_label | consolidation
    confidence: float
    evidence: str

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "entity": self.entity,
            "entity_type": self.entity_type,
            "method": self.method,
            "confidence": round(self.confidence, 2),
            "evidence": self.evidence,
        }


# --- Label store --------------------------------------------------------------

_labels: dict[str, dict] | None = None
_labels_mtime: float | None = None


def load_labels(force_reload: bool = False) -> dict[str, dict]:
    """
    Read data/labels.json into memory, re-reading it whenever the file changes.

    Keys are lowercased on load so a checksummed address from any source still
    matches. Underscore-prefixed keys are documentation, not data, and JSON has
    no comment syntax - hence the convention.

    WHY it watches the file's timestamp: labels are DATA, and uvicorn --reload
    only watches code. Without this, importing new labels appears to do nothing
    until someone thinks to restart the server - a trap that would be found
    mid-demo rather than now. Caching on mtime keeps lookups free while making
    an edit to labels.json take effect on the next request.
    """
    global _labels, _labels_mtime

    try:
        mtime = config.LABELS_PATH.stat().st_mtime
    except OSError:
        mtime = None

    if _labels is not None and not force_reload and mtime == _labels_mtime:
        return _labels
    _labels_mtime = mtime

    try:
        raw = json.loads(config.LABELS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # A missing labels file disables method (a) but must not break a trace;
        # the consolidation heuristic still works.
        _labels = {}
        return _labels
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"data/labels.json is not valid JSON: {exc}") from exc

    _labels = {
        str(addr).strip().lower(): meta
        for addr, meta in raw.items()
        if not str(addr).startswith("_") and isinstance(meta, dict)
    }
    return _labels


def label_count() -> int:
    """How many addresses we can currently recognise. Shown in the API response."""
    return len(load_labels())


# --- (a) Known-label lookup — WORKS -------------------------------------------


def known_label_lookup(address: str) -> Identification | None:
    """
    Method (a): is this address in our list of known entities?

    The highest-quality signal available. An exchange's hot wallets are public
    knowledge precisely because the exchange cannot operate secretly, so a
    direct hit names the entity outright rather than inferring it.

    Returns None when the address is unknown - which is the normal case, since
    most wallets in a trace are the criminal's own anonymous ones.
    """
    meta = load_labels().get(address.strip().lower())
    if meta is None:
        return None

    entity = str(meta.get("entity", "Unknown entity"))
    entity_type = str(meta.get("type", "unknown"))

    return Identification(
        address=address.strip().lower(),
        entity=entity,
        entity_type=entity_type,
        method="known_label",
        confidence=LABEL_CONFIDENCE,
        evidence=f"Exact match in labels.json: {entity} ({entity_type}).",
    )


def _as_store(graph):
    """
    Accept either a graph store or a bare NetworkX graph.

    The tracer passes a store (Neo4j or in-memory); tests and any ad-hoc
    analysis pass a DiGraph directly. Wrapping here means the identification
    logic below is written once and does not care which backend produced the
    graph - which is also what lets us prove the two backends agree.
    """
    if hasattr(graph, "wallet_count"):
        return graph
    store = graph_store.MemoryStore()
    store._g = graph  # noqa: SLF001 - deliberate adoption of the caller's graph
    return store


# --- (b) Deposit-consolidation clustering — WORKS -----------------------------


def consolidation_score(address: str, graph: nx.DiGraph) -> float:
    """
    Method (b): score how much this address looks like a collection point.

    THE FINGERPRINT: an exchange gives every customer their own deposit
    address, then sweeps them all into a small number of hot wallets. The
    result is a wallet with very high fan-in - many distinct senders paying one
    recipient - which is the inverse of an ordinary personal wallet, and it is
    structural. An exchange cannot avoid it without abandoning the deposit model
    its business runs on. That is what makes the pattern worth trusting even
    when the address is not on any label list.

    Returns 0.0 to 1.0, scaled between CONSOLIDATION_MIN_SENDERS and
    CONSOLIDATION_STRONG_SENDERS. Below the minimum the score is 0.0, because a
    handful of shared senders is ordinary and flagging it would produce false
    accusations.

    LIMITATION 1 - CRIMINALS CONSOLIDATE TOO. This is the serious one, and it
    is confirmed on real data: tracing the Ronin Bridge hacker flags an address
    that 6+ traced wallets funnel 84,963 ETH into, but its outgoing transaction
    count is ~1,600 against a real Binance hot wallet's ~18,000,000. It is the
    attacker re-pooling their own split funds, not an exchange sweeping customer
    deposits - and the two produce an identical fan-in signature. So a hit from
    this method is a LEAD, never an identification, and it must never be
    reported as a named exchange. The cheap discriminator, if this needs to get
    stronger later, is outgoing transaction count: exchange hot wallets exceed
    criminal wallets by three to four orders of magnitude, at a cost of one
    extra API call per candidate.

    LIMITATION 2: the fan-in counted here is IN-GRAPH only - distinct
    senders that this particular trace happened to walk. A forward BFS is close
    to a tree, so in-degree is usually 1 and only rises where laundering paths
    re-converge. That makes a positive result meaningful but a negative one
    weak: this measures convergence within the trace, not the address's true
    global fan-in. Reading real deposit consolidation requires fetching each
    candidate's INCOMING transactions from Etherscan and counting distinct
    senders chain-wide. That is the intended upgrade, and it costs one extra
    API call per candidate.
    """
    store = _as_store(graph)
    if not store.has(address):
        return 0.0

    # Distinct senders. The store collapses repeat payments between the same
    # pair into one edge, so this is a distinct-counterparty count by
    # construction. Self-loops do not count - a wallet paying itself is not a
    # third party, and both backends exclude them.
    fan_in = len(store.predecessors(address))

    if fan_in < CONSOLIDATION_MIN_SENDERS:
        return 0.0
    if fan_in >= CONSOLIDATION_STRONG_SENDERS:
        return 1.0

    span = CONSOLIDATION_STRONG_SENDERS - CONSOLIDATION_MIN_SENDERS
    return (fan_in - CONSOLIDATION_MIN_SENDERS) / span


def consolidation_fan_in(address: str, graph: nx.DiGraph) -> int:
    """Distinct third-party senders into this address, within the traced graph."""
    store = _as_store(graph)
    if not store.has(address):
        return 0
    return len(store.predecessors(address))


def adaptive_fan_in_floor(graph: nx.DiGraph, percentile: float = 0.95) -> int:
    """
    The fan-in a node must beat to count as UNUSUAL *for this particular graph*.

    WHY a fixed count is not enough: in a sparse trace (a thin laundering chain)
    six converging senders is remarkable. In a dense trace through high-volume
    infrastructure it is completely ordinary - measured on a real 402-node,
    1060-edge trace, a flat threshold of six flagged 34 separate addresses,
    which is not a finding, it is noise. Consolidation is a claim that an
    address is an OUTLIER, so the bar has to be read off the graph it sits in.
    """
    store = _as_store(graph)
    if store.wallet_count() == 0:
        return CONSOLIDATION_FLAG_SENDERS
    degrees = sorted(store.in_degrees())
    index = int(percentile * (len(degrees) - 1))
    return degrees[index]


def consolidation_identify(address: str, graph: nx.DiGraph) -> Identification | None:
    """
    Wrap the consolidation pattern into an Identification, or None if too weak.

    Two gates, both of which must pass. The absolute floor
    (CONSOLIDATION_FLAG_SENDERS) stops us calling three senders a pattern. The
    adaptive floor stops us calling an ordinary node in a dense graph an
    outlier. The gates are sender COUNTS, not the normalised score: scoring is
    for expressing confidence, but the decision to make an accusation at all
    should rest on a number an investigator can check by eye.
    """
    fan_in = consolidation_fan_in(address, graph)
    if fan_in < CONSOLIDATION_FLAG_SENDERS:
        return None
    if fan_in < adaptive_fan_in_floor(graph):
        return None

    score = consolidation_score(address, graph)
    confidence = CONSOLIDATION_MAX_CONFIDENCE * max(score, 0.5)

    return Identification(
        address=address.strip().lower(),
        entity="Unknown exchange (consolidation pattern)",
        # NOT "exchange" - we have not named anyone. This is a behavioural
        # suspicion, and the label keeps that distinction visible downstream.
        entity_type="suspected_exchange",
        method="consolidation",
        confidence=confidence,
        evidence=(
            f"{fan_in} distinct traced wallets funnel into this address "
            f"(fan-in score {score:.2f}; unusual for this graph, where the 95th "
            f"percentile is {adaptive_fan_in_floor(graph)}), matching the "
            f"deposit-consolidation pattern exchanges produce when sweeping "
            f"customer deposits. UNCONFIRMED - a criminal re-pooling their own "
            f"split funds produces the same shape."
        ),
    )


# --- (c) Behavioral classifier — STUB, future scope ---------------------------


def behavioral_classifier(address: str, graph: nx.DiGraph) -> None:
    """
    Method (c): classify a wallet from its transaction behaviour. NOT IMPLEMENTED.

    TODO (future scope). The intended approach: extract per-wallet features
    that separate institutional wallets from personal ones - transaction
    frequency, balance stability, round-number amounts, gas-price strategy,
    hour-of-day activity spread, in/out ratio, lifetime - and train a small
    supervised classifier on the labelled addresses in labels.json as ground
    truth.

    WHY IT WOULD HELP: it is the only method that can flag an exchange wallet
    that appears on no label list, which is exactly the gap for regional VASPs
    and freshly rotated hot wallets.

    WHY IT IS DEFERRED: it needs a labelled training set far larger than our
    starter file, and a weak classifier here produces confident false
    attributions - the most damaging error this tool can make in a law
    enforcement context. Methods (a) and (b) are deliberately prioritised.

    Always returns None, so callers can wire it in now and it stays inert.
    """
    return None


# --- (d) Co-spend clustering — STUB, not applicable to Ethereum ---------------


def cospend_cluster(address: str, graph: nx.DiGraph) -> None:
    """
    Method (d): cluster addresses by common spending. NOT IMPLEMENTED.

    TODO (future scope), and a note on why it is listed at all: co-spend (or
    "common input ownership") clustering is the workhorse of BITCOIN forensics.
    When one Bitcoin transaction spends several UTXOs as inputs, one private
    key signed for all of them, so they almost certainly share an owner.

    It does NOT transfer to Ethereum. Ethereum uses an account model, not
    UTXOs: a transaction has exactly one sender, so there is no multi-input
    event to infer shared ownership from. The Ethereum-appropriate substitutes
    are gas-funding analysis (one wallet seeding many with initial gas) and
    deposit-address reuse - both closer to method (b).

    Since this project is Ethereum-only, this method is N/A by design rather
    than merely unfinished. Always returns None.
    """
    return None


# --- Combined entry point -----------------------------------------------------


def identify(address: str, graph: nx.DiGraph) -> Identification | None:
    """
    Run the available methods against one address, best evidence first.

    Order matters: a label match names an actual company and outranks any
    statistical pattern, so it is checked first and returned immediately. The
    consolidation heuristic only speaks when the label list is silent.

    Returns None when nothing recognises the address - the expected outcome for
    the criminal's own wallets, and the reason the trace keeps walking.
    """
    hit = known_label_lookup(address)
    if hit is not None:
        return hit

    hit = consolidation_identify(address, graph)
    if hit is not None:
        return hit

    # Stubs, wired in so the pipeline is complete. Both return None today.
    if behavioral_classifier(address, graph) is not None:  # pragma: no cover
        pass
    if cospend_cluster(address, graph) is not None:  # pragma: no cover
        pass

    return None


def is_terminal(ident: Identification | None) -> bool:
    """
    Should the trace STOP expanding a wallet with this identification?

    Yes in three cases, each for its own reason:

      * exchange / suspected_exchange - we have arrived. This is the answer the
        investigation wanted, and walking past it would just enumerate the
        exchange's own internal shuffling.

      * mixer - the trail is broken by design. A mixer pool's outputs are
        deliberately unlinkable from its inputs, so following its outgoing
        transfers produces paths that look like evidence but are not. Stopping
        is the honest choice, and it also avoids expanding a wallet with tens of
        thousands of counterparties.

      * bridge - the funds have left Ethereum for another chain. We are
        Ethereum-only by scope, so there is nothing further to follow here.

    In all three cases the node is still recorded and flagged; only expansion
    stops.
    """
    if ident is None:
        return False
    return ident.entity_type in {"exchange", "suspected_exchange", "mixer", "bridge"}
