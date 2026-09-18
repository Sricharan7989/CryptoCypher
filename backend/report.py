"""
PDF report generation — the document an investigator actually files.

WHY A PDF AND NOT A SCREENSHOT
------------------------------
The finding has to leave this tool and travel into a process built on paper:
attached to a SAHYOG request, handed to a supervisor, disclosed to a defence
lawyer. That means it must be self-contained and self-justifying. Anyone who
opens it months later, with no access to this machine, has to be able to see
what was claimed, what it rests on, and how certain it was.

So the report carries the reasoning, not just the conclusion: the full
confidence arithmetic, the hop-by-hop path with transaction hashes anyone can
verify on a block explorer, and an explicit statement of the method's limits.
A report that printed "Binance, 88%" and nothing else would be unusable as
evidence and misleading as intelligence.
"""

from datetime import datetime, timezone
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Muted, printable palette. A report that comes out of a station printer in
# greyscale still has to be readable, so meaning never rests on colour alone.
INK = colors.HexColor("#0f172a")
SOFT = colors.HexColor("#475569")
FAINT = colors.HexColor("#94a3b8")
LINE = colors.HexColor("#cbd5e1")
GOOD = colors.HexColor("#15803d")
WARN = colors.HexColor("#c2410c")
DANGER = colors.HexColor("#b91c1c")
BAND = colors.HexColor("#f1f5f9")


def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=17, leading=21,
            textColor=INK, alignment=TA_LEFT, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=9, textColor=FAINT, spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=10.5, leading=13, textColor=INK,
            spaceBefore=13, spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=9.5, leading=13, textColor=INK,
        ),
        "small": ParagraphStyle(
            "small", parent=base["Normal"], fontSize=8, leading=11, textColor=SOFT,
        ),
        "mono": ParagraphStyle(
            "mono", parent=base["Normal"], fontSize=7.5, leading=10,
            fontName="Courier", textColor=INK,
        ),
        "headline": ParagraphStyle(
            "headline", parent=base["Normal"], fontSize=14, leading=18,
            textColor=INK, spaceBefore=3, spaceAfter=3,
        ),
        "disclaimer": ParagraphStyle(
            "disclaimer", parent=base["Normal"], fontSize=7.5, leading=10.5, textColor=SOFT,
        ),
    }


def _hex(colour) -> str:
    """'#rrggbb' for inline <font color=...> markup - reportlab rejects it bare."""
    return "#" + colour.hexval()[2:]


def _fmt_eth(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if v >= 1000:
        return f"{v:,.0f} ETH"
    if v >= 1:
        return f"{v:,.2f} ETH"
    return f"{v:.4f} ETH"


def _short(address: str) -> str:
    return f"{address[:10]}…{address[-8:]}" if address and len(address) > 20 else (address or "")


def _kv_table(rows: list[tuple[str, str]], styles: dict) -> Table:
    data = [[Paragraph(k, styles["small"]), Paragraph(v, styles["body"])] for k, v in rows]
    table = Table(data, colWidths=[38 * mm, 128 * mm])
    table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, LINE),
        ])
    )
    return table


def _rule() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.6, color=LINE, spaceBefore=6, spaceAfter=6)


def _path_section(payload: dict, styles: dict) -> list:
    """The hop-by-hop trail, with a verifiable transaction hash on every step."""
    summary = payload.get("summary", {})
    target = summary.get("address")

    attribution = next(
        (a for a in payload.get("attributions", []) if a.get("address") == target), None
    )
    path = (attribution or {}).get("path") or []
    if not path:
        return [Paragraph("No route could be reconstructed.", styles["body"])]

    nodes = {n["id"]: n for n in payload.get("nodes", [])}
    edges = {(e["source"], e["target"]): e for e in payload.get("edges", [])}

    head = ["Hop", "Role", "Address", "Value received", "Transaction"]
    rows = [[Paragraph(f"<b>{h}</b>", styles["small"]) for h in head]]

    for index, address in enumerate(path):
        node = nodes.get(address, {})
        if index == 0:
            role = "Suspect wallet"
        elif node.get("entity_type") == "suspected_exchange":
            role = "Possible collection point"
        elif node.get("is_vasp"):
            role = node.get("label") or "Exchange"
        elif node.get("is_mixer"):
            role = f"{node.get('label')} (mixer)"
        elif node.get("is_bridge"):
            role = f"{node.get('label')} (bridge)"
        else:
            role = "Intermediate wallet"

        edge = edges.get((path[index - 1], address)) if index > 0 else None
        rows.append([
            Paragraph(str(index) if index else "—", styles["small"]),
            Paragraph(role, styles["body"]),
            Paragraph(address, styles["mono"]),
            Paragraph(_fmt_eth(edge["value_eth"]) if edge else "—", styles["small"]),
            Paragraph(_short(edge["tx_hash"]) if edge else "—", styles["mono"]),
        ])

    table = Table(rows, colWidths=[10 * mm, 34 * mm, 62 * mm, 26 * mm, 34 * mm], repeatRows=1)
    table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, LINE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    return [table]


def _confidence_section(summary: dict, styles: dict) -> list:
    """
    The score AND the arithmetic behind it.

    Printing the number alone would make it unchallengeable, which is precisely
    what a figure used to justify a legal request must never be.
    """
    components = summary.get("confidence_components") or []
    score = summary.get("confidence_score", 0)

    if not components:
        return [Paragraph(f"Confidence: {score}%", styles["body"])]

    rows = [[Paragraph("<b>Factor</b>", styles["small"]),
             Paragraph("<b>Points</b>", styles["small"])]]
    for component in components:
        points = component.get("points", 0)
        colour = DANGER if points < 0 else GOOD
        rows.append([
            Paragraph(str(component.get("label", "")), styles["body"]),
            Paragraph(
                f'<font color="{_hex(colour)}">{points:+d}</font>', styles["body"]
            ),
        ])
    rows.append([
        Paragraph("<b>Confidence score</b>", styles["body"]),
        Paragraph(f"<b>{score} / 100</b>", styles["body"]),
    ])

    table = Table(rows, colWidths=[130 * mm, 36 * mm])
    table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, LINE),
            ("LINEABOVE", (0, -1), (-1, -1), 0.6, LINE),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    return [table, Spacer(1, 4),
            Paragraph(
                "Scores are capped at 95. This tool never asserts certainty.",
                styles["small"],
            )]


def _risk_section(payload: dict, styles: dict) -> list:
    flags = payload.get("risk_flags") or []
    if not flags:
        return [Paragraph(
            "No mixers, bridges, scam or sanctioned addresses were identified "
            "in this trace.", styles["body"],
        )]

    rows = [[Paragraph(f"<b>{h}</b>", styles["small"])
             for h in ["Severity", "Entity", "On path", "Hop", "Value received"]]]
    for flag in flags:
        severity = flag.get("severity", "")
        colour = DANGER if severity == "critical" else WARN if severity == "high" else SOFT
        rows.append([
            Paragraph(
                f'<font color="{_hex(colour)}"><b>{severity.upper()}</b></font>',
                styles["small"],
            ),
            Paragraph(str(flag.get("entity", "")), styles["body"]),
            Paragraph("Yes" if flag.get("on_primary_path") else "No", styles["small"]),
            Paragraph(str(flag.get("hop_distance", "")), styles["small"]),
            Paragraph(_fmt_eth(flag.get("value_received_eth")), styles["small"]),
        ])

    table = Table(rows, colWidths=[20 * mm, 76 * mm, 18 * mm, 12 * mm, 40 * mm], repeatRows=1)
    table.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, LINE),
            ("LINEBELOW", (0, 1), (-1, -2), 0.25, LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ])
    )

    notes = [Spacer(1, 5)]
    for flag in flags:
        if flag.get("on_primary_path"):
            notes.append(Paragraph(f"• {flag.get('note', '')}", styles["small"]))
    return [table, *notes]


DISCLAIMER = (
    "<b>Basis and limitations.</b> This report is derived entirely from public "
    "Ethereum transaction records. No cryptography was broken, no private data "
    "was accessed, and no individual was identified by this tool. It establishes "
    "that funds moved along the path shown and arrived at a wallet attributed to "
    "the named entity; it does NOT establish who controlled any intermediate "
    "wallet. Attribution of the endpoint rests on the method stated above and "
    "carries the confidence score shown, which is never certainty. "
    "Native ETH transfers only: transfers of ERC-20 tokens such as USDT, and "
    "internal contract transfers, are not followed in this version, so the trail "
    "may continue beyond what is shown. Identity can only be established by the "
    "named exchange, from its own KYC records, in response to a lawful request."
)


def build_report(payload: dict) -> bytes:
    """
    Render a completed trace into a PDF and return the raw bytes.

    Takes the same JSON dict the /trace endpoint serves, so the report can be
    generated from a live trace or a cached replay with no difference in output.
    """
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=22 * mm, rightMargin=22 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="Cryptocurrency Attribution Report",
        author="VASP Attribution Engine",
    )

    summary = payload.get("summary", {})
    stats = payload.get("stats", {})
    params = payload.get("params", {})
    generated = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")

    story: list = []

    # --- Header ---------------------------------------------------------
    story.append(Paragraph("Cryptocurrency Attribution Report", styles["title"]))
    story.append(Paragraph(
        "Wallet-to-VASP tracing · prepared for lawful request via SAHYOG / I4C",
        styles["subtitle"],
    ))
    story.append(_rule())

    source = payload.get("source", "live")
    source_text = "Live blockchain query"
    if source == "cache":
        recorded = payload.get("recorded_at", "unknown time")
        source_text = f"Recorded trace (captured {recorded})"

    story.append(_kv_table([
        ("Suspect address", f'<font face="Courier" size="8">{payload.get("start_address", "")}</font>'),
        ("Report generated", generated),
        ("Data source", f"Ethereum mainnet via Etherscan · {source_text}"),
        ("Trace depth", f'{params.get("max_depth", "?")} hops '
                        f'(dust threshold {params.get("dust_threshold_eth", "?")} ETH)'),
        ("Wallets examined", f'{stats.get("nodes", 0)} wallets, {stats.get("edges", 0)} transfers'),
    ], styles))

    # --- Finding --------------------------------------------------------
    story.append(Paragraph("Finding", styles["h2"]))

    if summary.get("found"):
        story.append(Paragraph(
            f'<b>Funds reached {summary.get("exchange")}</b>, '
            f'{summary.get("hop_distance")} hop'
            f'{"" if summary.get("hop_distance") == 1 else "s"} from the suspect '
            f'address, at {summary.get("confidence_score")}% confidence.',
            styles["headline"],
        ))
        story.append(Spacer(1, 3))
        story.append(_kv_table([
            ("Exchange", str(summary.get("exchange", ""))),
            ("Exchange wallet",
             f'<font face="Courier" size="8">{summary.get("address", "")}</font>'),
            ("Value traced in", _fmt_eth(summary.get("value_received_eth"))),
            ("Identification method",
             "Direct match against known exchange wallets"
             if summary.get("method") == "known_label"
             else "Deposit-consolidation pattern (unconfirmed)"),
        ], styles))
    elif summary.get("lead"):
        story.append(Paragraph(
            f'<b>No named exchange was reached within {params.get("max_depth")} hops.</b> '
            f'A possible collection point was identified '
            f'{summary.get("hop_distance")} hops away at '
            f'{summary.get("confidence_score")}% confidence. '
            f'<font color="#b91c1c">This is an UNCONFIRMED lead, not an '
            f'identified exchange.</font>',
            styles["headline"],
        ))
        story.append(Spacer(1, 3))
        story.append(_kv_table([
            ("Address of interest",
             f'<font face="Courier" size="8">{summary.get("address", "")}</font>'),
            ("Value traced in", _fmt_eth(summary.get("value_received_eth"))),
            ("Caution", "A criminal re-pooling their own split funds produces the "
                        "same fan-in pattern as an exchange sweeping customer "
                        "deposits. Verify independently before acting."),
        ], styles))
    else:
        story.append(Paragraph(
            f'<b>No known exchange was reached within '
            f'{params.get("max_depth")} hops of the suspect address.</b>',
            styles["headline"],
        ))

    # --- Confidence -----------------------------------------------------
    if summary.get("found") or summary.get("lead"):
        story.append(Paragraph("Confidence assessment", styles["h2"]))
        story.extend(_confidence_section(summary, styles))

    # --- Path -----------------------------------------------------------
    story.append(Paragraph("Traced path", styles["h2"]))
    story.extend(_path_section(payload, styles))

    # --- Risk flags -----------------------------------------------------
    story.append(Paragraph("Risk flags", styles["h2"]))
    story.extend(_risk_section(payload, styles))

    # --- Recommended action ---------------------------------------------
    action = summary.get("recommended_action", "")
    if action:
        story.append(Paragraph("Recommended action", styles["h2"]))
        story.append(KeepTogether([
            Paragraph(action, styles["body"]),
        ]))

    # --- Disclaimer -----------------------------------------------------
    story.append(Spacer(1, 10))
    story.append(_rule())
    story.append(Paragraph(DISCLAIMER, styles["disclaimer"]))

    def _footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(FAINT)
        canvas.drawString(
            22 * mm, 11 * mm,
            f"VASP Attribution Engine · {payload.get('start_address', '')[:18]}… · {generated}",
        )
        canvas.drawRightString(A4[0] - 22 * mm, 11 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def filename_for(address: str) -> str:
    """Stable, sortable filename an investigator can file without renaming."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"attribution-report-{address[:10]}-{stamp}.pdf"
