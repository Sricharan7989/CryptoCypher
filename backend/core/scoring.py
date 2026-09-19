"""
Confidence scoring — how much weight an attribution deserves, and why.

WHY THIS IS A WEIGHTED SUM AND NOT A MODEL
------------------------------------------
This number can justify a legal request against a real person. An investigator,
a defence lawyer and a judge all have to be able to interrogate it, so every
point must be traceable to a stated reason. A trained model that emitted 0.87
with no account of itself would be worse than useless here - it would be
unchallengeable. So the score is a small table of named weights that anyone can
read, argue with, and adjust.

The three factors, in order of how much they move the number:

  1. HOW it was identified. A published exchange label is direct evidence of
     ownership. A consolidation pattern is only a behavioural resemblance, and
     a criminal re-pooling their own funds produces the same shape - so it earns
     less than half the weight.

  2. HOW FAR the money travelled. Each extra hop is another wallet whose
     ownership we did not establish. At one hop the suspect paid the exchange
     directly; at five, the link is an inference across five unknown parties.

  3. WHAT IT PASSED THROUGH. A mixer breaks the chain of custody by design - it
     deliberately severs the link between its inputs and outputs, so a path
     crossing one is materially weaker evidence and takes the largest single
     penalty in the table. A bridge is less damaging but still moves funds off
     Ethereum and outside what we observed.

THE CEILING IS 95, NOT 100
--------------------------
Deliberate. Labels go stale, exchanges rotate wallets, and published lists are
sometimes simply wrong. A tool that can print "100% confident" invites someone
to stop thinking, and this one must never do that.
"""

from dataclasses import dataclass, field

# --- Weights (the entire model lives here, on purpose) ------------------------

# Factor 1: identification method.
METHOD_POINTS = {
    "known_label": 50,
    "consolidation": 22,
}
METHOD_LABELS = {
    "known_label": "direct label match",
    "consolidation": "consolidation pattern only",
}

# Factor 2: hop distance. Full marks at one hop, decaying by HOP_DECAY each hop.
HOP_MAX_POINTS = 30
HOP_DECAY = 7

# Factor 3: what the path crossed.
CLEAN_PATH_POINTS = 15  # awarded only when nothing obfuscating was crossed
MIXER_PENALTY = -30
BRIDGE_PENALTY = -12

# A score may never reach certainty, and may never fall to zero either - a
# recognised endpoint is always worth something, even a weak one.
MAX_SCORE = 95
MIN_SCORE = 5

# Label types that constitute a risk worth surfacing on its own.
# "scam" and "sanctioned" are supported here but are NOT currently present in
# labels.json: they must come from an authoritative source (the OFAC SDN list,
# a vetted TagPack), never from memory. Sanctions status also changes - Tornado
# Cash was designated in 2022 and delisted in 2025 - so hardcoding it would
# eventually make this tool assert something false about a real entity.
RISK_SEVERITY = {
    "sanctioned": "critical",
    "scam": "critical",
    "mixer": "high",
    "bridge": "medium",
}


@dataclass
class ScoreComponent:
    """One named, signed contribution to the final score."""

    label: str
    points: int

    def to_dict(self) -> dict:
        return {"label": self.label, "points": self.points}

    def __str__(self) -> str:
        return f"{self.label} ({self.points:+d})"


@dataclass
class ConfidenceScore:
    """A score plus the full arithmetic that produced it."""

    score: int  # 0-100
    components: list[ScoreComponent] = field(default_factory=list)
    capped: bool = False

    @property
    def breakdown(self) -> str:
        """One line an investigator can read aloud: 'direct label match (+50), …'."""
        text = ", ".join(str(c) for c in self.components)
        if self.capped:
            text += f", capped at {MAX_SCORE} (certainty is never claimed)"
        return text

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "breakdown": self.breakdown,
            "components": [c.to_dict() for c in self.components],
        }


def hop_points(hop_distance: int) -> int:
    """
    Points for proximity. One hop is full marks; each further hop costs HOP_DECAY.

    Never negative: distance weakens an attribution but does not by itself make
    it evidence against the finding.
    """
    if hop_distance <= 0:
        return HOP_MAX_POINTS
    return max(0, HOP_MAX_POINTS - (hop_distance - 1) * HOP_DECAY)


def compute_confidence(attribution) -> ConfidenceScore:
    """
    Score one attribution from 0-100, with a line-by-line explanation.

    Reads three things off the attribution: `method`, `hop_distance`, and
    `path_risk_types` (the set of label types crossed on the way, filled in by
    the tracer). Everything else is the weight table above.

    The returned ConfidenceScore carries its own arithmetic, so the API can show
    the reasoning rather than asking anyone to trust the number.
    """
    components: list[ScoreComponent] = []

    method = getattr(attribution, "method", "") or ""
    hops = int(getattr(attribution, "hop_distance", 0) or 0)
    crossed = set(getattr(attribution, "path_risk_types", ()) or ())

    # Factor 1 - identification method.
    method_points = METHOD_POINTS.get(method, 10)
    method_label = METHOD_LABELS.get(method, f"identified by {method or 'unknown method'}")
    components.append(ScoreComponent(method_label, method_points))

    # Factor 2 - hop distance.
    points = hop_points(hops)
    hop_label = f"{hops} hop{'s' if hops != 1 else ''} from suspect"
    components.append(ScoreComponent(hop_label, points))

    # Factor 3 - what the path crossed. Mixers and bridges are penalised
    # separately because they damage the chain of custody to different degrees.
    if "mixer" in crossed:
        components.append(ScoreComponent("mixer on path (chain of custody broken)", MIXER_PENALTY))
    if "bridge" in crossed:
        components.append(ScoreComponent("bridge on path (funds left Ethereum)", BRIDGE_PENALTY))
    if "sanctioned" in crossed:
        # Not a confidence penalty: a sanctioned hop does not make the trace
        # less reliable. It is surfaced as a risk flag instead.
        pass
    if not crossed:
        components.append(ScoreComponent("no mixer or bridge on path", CLEAN_PATH_POINTS))

    raw = sum(c.points for c in components)
    capped = raw > MAX_SCORE
    score = max(MIN_SCORE, min(MAX_SCORE, raw))

    return ConfidenceScore(score=score, components=components, capped=capped)


def risk_severity(entity_type: str) -> str | None:
    """How serious a label type is as a risk flag, or None if it is not one."""
    return RISK_SEVERITY.get((entity_type or "").lower())


def risk_note(entity_type: str, entity: str) -> str:
    """Plain-language explanation of why this wallet is flagged."""
    kind = (entity_type or "").lower()
    if kind == "mixer":
        return (
            f"{entity} is a mixing service. It deliberately severs the link "
            f"between funds going in and coming out, so the trail cannot be "
            f"followed past this point on-chain."
        )
    if kind == "bridge":
        return (
            f"{entity} moves funds to another blockchain. The money continues "
            f"outside Ethereum, beyond what this tool traces."
        )
    if kind == "sanctioned":
        return (
            f"{entity} appears on a sanctions list. Handling these funds may "
            f"carry legal obligations of its own - escalate before acting."
        )
    if kind == "scam":
        return f"{entity} is recorded as a known fraudulent or scam-linked address."
    return f"{entity} is flagged as {kind}."
