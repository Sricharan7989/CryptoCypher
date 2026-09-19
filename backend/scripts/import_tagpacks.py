"""
Import address labels from the GraphSense TagPacks into data/labels.json.

    python -m scripts.import_tagpacks            # fetch, filter, verify on-chain, merge
    python -m scripts.import_tagpacks --dry-run  # show what would change, write nothing
    python -m scripts.import_tagpacks --no-verify  # skip the on-chain check (faster, riskier)

WHY THIS IS NOT A STRAIGHT COPY
-------------------------------
A label file is the ground truth for method (a), so a wrong entry here does not
produce a small error - it produces a CONFIDENT, FALSE attribution at 88%
confidence, with a recommendation to serve a legal request on whoever it names.
That is the worst thing this tool can do. Bulk-importing a third-party list
without inspecting it would trade a small, trustworthy label set for a large,
untrustworthy one.

And the TagPacks genuinely need inspecting. `etherscan-wordcloud-exchange.yaml`
carries 646 addresses tagged "exchange", but they are scraped from Etherscan's
label cloud, where the tag is applied loosely: 267 of them (41%) are ERC-20
TOKEN CONTRACTS of exchange-affiliated tokens - "AAX Token (AAB)",
"2GT_token (2GT)" - not exchange wallets at all. A token contract holds no
customer deposits, has no KYC records, and cannot answer a SAHYOG request.

So every candidate passes two filters:

  1. LABEL SHAPE - reject anything that reads like a token: a trailing ticker
     in parentheses, or the word "token". Cheap, and catches the bulk of it.

  2. ON-CHAIN REALITY - for exchange entries, reject addresses that are
     contracts with no outgoing transaction history. An exchange hot wallet is
     a live spending wallet with a large nonce; a token contract sends nothing
     itself. This catches the token contracts whose labels look innocent.

Mixers are deliberately NOT held to filter 2: a mixer pool is SUPPOSED to be a
contract, so requiring an EOA there would reject every genuine entry.

Existing entries in labels.json always win. They were verified by hand, and an
import must never silently overwrite a checked fact with a scraped one.
"""

import argparse
import asyncio
import base64
import json
import re
import sys
import urllib.request

import httpx
import yaml

from app import config

GITHUB_API = (
    "https://api.github.com/repos/graphsense/graphsense-tagpacks/contents/packs/"
)

# Packs to import, and the type each contributes. Chosen deliberately, not
# wholesale - see the notes beside each exclusion at the bottom of this file.
PACKS = {
    "etherscan-wordcloud-exchange.yaml": "exchange",
    "etherscan-wordcloud-mixing_service.yaml": "mixer",
    "tornado_cash.yaml": "mixer",
    "exchange-wallets-binance.yaml": "exchange",
    "exchange-wallets-bitfinexcom.yaml": "exchange",
    "exchange-wallets-bybit.yaml": "exchange",
    "exchange-wallets-cryptocom.yaml": "exchange",
    "exchange-wallets-deribit.yaml": "exchange",
    "exchange-wallets-huobi.yaml": "exchange",
    "exchange-wallets-kucoin.yaml": "exchange",
    "exchange-wallets-okx.yaml": "exchange",
    "exchange-wallets-swissborg.yaml": "exchange",
}

# GraphSense category -> our label type.
CATEGORY_MAP = {
    "exchange": "exchange",
    "mixing_service": "mixer",
    "bridge": "bridge",
}

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# A trailing "(TICKER)", or the word "token" anywhere: both say ERC-20 contract.
TOKEN_RE = re.compile(r"\(\s*\w{2,10}\s*\)\s*$")

# Trailing wallet index, e.g. "Binance 14" -> "Binance". The company is what an
# investigator serves a request on; which of its hot wallets it was is already
# recorded separately as the address.
INDEX_RE = re.compile(r"[\s_-]+\d+$")

# An exchange hot wallet sends constantly. Below this many outgoing
# transactions an address is not carrying exchange flow, whatever it is called.
MIN_NONCE_FOR_EXCHANGE = 25


def looks_like_token(label: str) -> bool:
    low = label.lower()
    return bool(TOKEN_RE.search(label)) or "token" in low or "voucher" in low


def clean_entity(label: str) -> str:
    """Tidy a TagPack label into an entity name fit for an investigator's report."""
    name = label.strip().strip("'\"")
    name = re.sub(r"\s*:\s*", ": ", name)
    name = INDEX_RE.sub("", name)
    return name.strip() or label.strip()


def fetch_pack(filename: str) -> str:
    """
    Download one pack through the GitHub contents API.

    The API rather than raw.githubusercontent.com on purpose: the raw host
    returns empty bodies from some networks (including this one), which would
    look like an empty pack rather than a failed download.
    """
    request = urllib.request.Request(
        GITHUB_API + filename,
        headers={"User-Agent": "vasp-attribution-engine", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        body = json.load(response)

    if body.get("encoding") != "base64" or not body.get("content"):
        raise RuntimeError(
            f"{filename}: no inline content (file may exceed the API's 1 MB limit)"
        )
    return base64.b64decode(body["content"]).decode("utf-8", "replace")


def parse_pack(text: str, filename: str, default_type: str) -> tuple[list[dict], dict]:
    """
    Turn one pack's YAML into candidate label rows, with a rejection tally.

    TagPack fields cascade: a tag inherits `currency`, `label` and `category`
    from the pack header unless it overrides them. Ethereum-only is enforced
    here, since several packs mix BTC and ETH addresses in one file.
    """
    document = yaml.safe_load(text) or {}
    header_currency = str(document.get("currency", "") or "").upper()
    header_label = str(document.get("label") or document.get("title") or "").strip()
    header_category = str(document.get("category", "") or "")

    rejected = {"not_eth": 0, "bad_address": 0, "token_like": 0, "no_label": 0}
    rows: list[dict] = []

    for tag in document.get("tags") or []:
        if not isinstance(tag, dict):
            continue

        address = str(tag.get("address", "")).strip().strip("'\"")
        currency = str(tag.get("currency", header_currency) or "").upper()

        if currency != "ETH":
            rejected["not_eth"] += 1
            continue
        if not ADDRESS_RE.match(address):
            # Non-Ethereum address shapes (BTC, bech32) land here too.
            rejected["bad_address"] += 1
            continue

        label = str(tag.get("label") or header_label or "").strip()
        if not label:
            rejected["no_label"] += 1
            continue
        if looks_like_token(label):
            rejected["token_like"] += 1
            continue

        category = str(tag.get("category", header_category) or "")
        entity_type = CATEGORY_MAP.get(category, default_type)

        rows.append(
            {
                "address": address.lower(),
                "entity": clean_entity(label),
                "type": entity_type,
                "source": filename,
            }
        )

    return rows, rejected


async def verify_on_chain(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Keep only exchange candidates that behave like live exchange wallets.

    One eth_getCode plus one eth_getTransactionCount per address, throttled to
    the free tier. A contract that has never sent a transaction is a token or
    protocol contract, not a wallet holding customer deposits - and those are
    exactly the entries that would otherwise produce a false attribution.

    Mixer and bridge rows skip this: they are legitimately contracts.
    """
    need_check = [r for r in rows if r["type"] == "exchange"]
    passthrough = [r for r in rows if r["type"] != "exchange"]

    if not config.has_etherscan_key():
        print("  ! ETHERSCAN_API_KEY missing - skipping on-chain verification")
        return rows, []

    kept, dropped = list(passthrough), []
    total = len(need_check)
    print(f"  verifying {total} exchange candidates on-chain "
          f"(~{total * 2 * config.ETHERSCAN_REQUEST_DELAY_SEC / 60:.1f} min)…")

    async with httpx.AsyncClient(timeout=45) as client:
        async def rpc(action: str, address: str):
            params = {
                "chainid": config.ETHERSCAN_CHAIN_ID, "module": "proxy",
                "action": action, "address": address, "tag": "latest",
                "apikey": config.ETHERSCAN_API_KEY,
            }
            response = await client.get(config.ETHERSCAN_BASE_URL, params=params)
            await asyncio.sleep(config.ETHERSCAN_REQUEST_DELAY_SEC)
            body = response.json()
            if str(body.get("status", "")) == "0":
                raise RuntimeError(str(body.get("result")))
            return body.get("result")

        for index, row in enumerate(need_check, 1):
            if index % 50 == 0:
                print(f"    {index}/{total} checked…")
            try:
                code = await rpc("eth_getCode", row["address"])
                nonce_hex = await rpc("eth_getTransactionCount", row["address"])
                nonce = int(nonce_hex, 16)
            except Exception as exc:  # noqa: BLE001 - a lookup failure is not a verdict
                row["reason"] = f"lookup failed ({exc})"
                dropped.append(row)
                continue

            is_contract = bool(code) and code != "0x"
            if is_contract and nonce == 0:
                row["reason"] = "contract that has never sent a transaction (token/protocol)"
                dropped.append(row)
            elif nonce < MIN_NONCE_FOR_EXCHANGE:
                row["reason"] = f"only {nonce} outgoing txs - not exchange-scale"
                dropped.append(row)
            else:
                row["nonce"] = nonce
                kept.append(row)

    return kept, dropped


def merge(rows: list[dict], dry_run: bool) -> dict:
    """
    Fold accepted rows into labels.json, leaving hand-verified entries untouched.

    Deduplication is on the lowercased address. An address already present is
    left exactly as it is - the existing file was checked on-chain by hand, and
    a scraped label must never quietly replace a verified one.
    """
    raw = json.loads(config.LABELS_PATH.read_text(encoding="utf-8"))
    existing_keys = {k.lower() for k in raw if not k.startswith("_")}

    stats = {"before": len(existing_keys), "added": 0, "already_present": 0, "dupes_in_feed": 0}
    seen: set[str] = set()
    # Count each already-known address once, however many packs mention it.
    hit_existing: set[str] = set()
    additions: dict[str, dict] = {}

    for row in rows:
        address = row["address"]
        if address in existing_keys:
            hit_existing.add(address)
            continue
        if address in seen:
            stats["dupes_in_feed"] += 1
            continue
        seen.add(address)
        additions[address] = {
            "entity": row["entity"],
            "type": row["type"],
            "source": "graphsense-tagpacks",
        }
        stats["added"] += 1

    stats["already_present"] = len(hit_existing)
    stats["after"] = stats["before"] + stats["added"]

    if not dry_run and additions:
        raw.update(additions)
        config.LABELS_PATH.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")

    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--no-verify", action="store_true", help="skip the on-chain check")
    parser.add_argument(
        "--packs", nargs="+", metavar="FILE",
        help="import only these pack filenames (for retrying ones that failed)",
    )
    args = parser.parse_args()

    # The GitHub contents API allows 60 unauthenticated calls an hour, so a
    # pack can fail with a 403 through no fault of the data. Re-running is safe
    # (existing entries always win); this just avoids re-verifying everything.
    packs = PACKS
    if args.packs:
        unknown = [p for p in args.packs if p not in PACKS]
        if unknown:
            print(f"error: not in PACKS: {', '.join(unknown)}", file=sys.stderr)
            return 2
        packs = {p: PACKS[p] for p in args.packs}

    print(f"Fetching {len(PACKS)} packs from graphsense-tagpacks…\n")
    print(f"{'pack':<42} {'kept':>6} {'non-ETH':>8} {'token-like':>11}")
    print("-" * 71)

    candidates: list[dict] = []
    for filename, default_type in packs.items():
        try:
            text = fetch_pack(filename)
        except Exception as exc:  # noqa: BLE001
            print(f"{filename:<42}  FAILED: {exc}")
            continue
        rows, rejected = parse_pack(text, filename, default_type)
        candidates.extend(rows)
        print(f"{filename:<42} {len(rows):>6} {rejected['not_eth'] + rejected['bad_address']:>8} "
              f"{rejected['token_like']:>11}")

    print("-" * 71)
    print(f"{'candidates after label filter':<42} {len(candidates):>6}\n")

    if not candidates:
        print("Nothing to import.")
        return 1

    dropped: list[dict] = []
    if not args.no_verify:
        candidates, dropped = await verify_on_chain(candidates)
        print(f"\n  on-chain check: kept {len(candidates)}, dropped {len(dropped)}")
        for row in dropped[:8]:
            print(f"    - {row['entity'][:34]:<34} {row.get('reason', '')}")
        if len(dropped) > 8:
            print(f"    … and {len(dropped) - 8} more")

    stats = merge(candidates, args.dry_run)

    print()
    print("=" * 71)
    print(f"  labels before      : {stats['before']}")
    print(f"  new addresses added: {stats['added']}")
    print(f"  already present    : {stats['already_present']} (kept the existing entry)")
    print(f"  duplicate in feed  : {stats['dupes_in_feed']}")
    print(f"  LABELS NOW         : {stats['after']}")
    print("=" * 71)
    if args.dry_run:
        print("\n(dry run - data/labels.json was not modified)")
    return 0


# PACKS DELIBERATELY EXCLUDED
# ---------------------------
# ofac.yaml (572 tags) - would be the authoritative source for a "sanctioned"
#   type, but its lastmod is 2024-02-26. OFAC delisted Tornado Cash in March
#   2025, so importing this today would make the tool assert a sanctions status
#   that no longer holds against a real entity. Sanctions data must be pulled
#   live from the SDN list, not from a snapshot.
# ronin_bridge.yaml - despite the name, its tags are the Ronin ATTACKER's
#   wallets ("Ronin bridge exploiter"), not bridge infrastructure. Importing
#   them as type "bridge" would be simply wrong, and it would also label our
#   own demo start address.
# blender_io.yaml, sinbad_io.yaml, samourai.yaml, wasabi_collector.yaml -
#   Bitcoin mixers. This project is Ethereum-only.
# exchange-wallets-bitmex_*.yaml - 2.3 MB each and overwhelmingly BTC; they
#   also exceed the GitHub contents API's inline size limit.

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
