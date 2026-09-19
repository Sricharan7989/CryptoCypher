"""
Etherscan data layer — fetches a wallet's transaction history, with caching.

WHY this is a separate module:
The tracer's job is graph logic. Everything ugly about talking to a third-party
API — rate limits, pagination, the fact that Etherscan reports errors with
HTTP 200 and a status field, wei-to-ETH conversion — is quarantined here. The
tracer asks one question, "what left this wallet?", and gets back clean data.

WHY the cache matters:
A forward trace revisits the same wallet constantly. Criminal flows loop and
re-converge — wallet A pays B and C, and both pay D — so a naive tracer would
refetch D once per inbound path. Every refetch is a network round trip against
a 5-calls/sec free tier, so the cache is the difference between a trace that
takes seconds and one that takes minutes. It is keyed by lowercased address and
lives for the process, so the demo address stays warm between traces.
"""

import asyncio
import time
from dataclasses import dataclass

import httpx

from app import config

# Ethereum addresses are 20 bytes / 40 hex chars, plus the "0x" prefix.
# Etherscan returns them EIP-55 checksummed (mixed case) but treats them
# case-insensitively; we lowercase everywhere so the graph never holds the
# same wallet twice under two spellings.
ADDRESS_LENGTH = 42


class EtherscanError(RuntimeError):
    """Upstream refused or failed. Surfaced to the caller as a 502, never swallowed."""


@dataclass(frozen=True)
class Transfer:
    """One value-bearing ETH transaction, normalised out of Etherscan's raw JSON."""

    hash: str
    from_addr: str
    to_addr: str
    value_eth: float
    timestamp: int  # unix epoch seconds
    block: int


def normalize_address(address: str) -> str:
    """Lowercase + strip. The single source of truth for how an address is keyed."""
    return address.strip().lower()


def is_valid_address(address: str) -> bool:
    """Shape check only — does not verify the address exists on chain."""
    a = address.strip()
    if len(a) != ADDRESS_LENGTH or not a.startswith("0x"):
        return False
    try:
        int(a[2:], 16)
    except ValueError:
        return False
    return True


class EtherscanClient:
    """
    Async Etherscan client with an in-memory cache and a request throttle.

    The throttle serialises requests rather than running a token bucket on
    purpose: exceeding the free tier's 5 calls/sec gets the key temporarily
    blocked, and a blocked key mid-demo is far worse than a trace that takes
    an extra second.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # address -> transfers sent BY that address
        self._cache: dict[str, list[Transfer]] = {}
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self.api_calls = 0
        self.cache_hits = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def reset_stats(self) -> None:
        """Zero the per-trace counters. The cache itself deliberately survives."""
        self.api_calls = 0
        self.cache_hits = 0

    def clear_cache(self) -> None:
        self._cache.clear()

    async def _request(self, params: dict) -> object:
        """
        One throttled Etherscan V2 call.

        Etherscan signals failure with HTTP 200 and status="0", so the response
        body has to be inspected rather than trusting the status code. The one
        benign "failure" is "No transactions found" — a wallet with no outgoing
        history is a normal dead end in a trace, not an error.
        """
        if not config.has_etherscan_key():
            raise EtherscanError(
                "ETHERSCAN_API_KEY is not set in backend/.env - cannot run a live trace."
            )

        query = {
            **params,
            "chainid": config.ETHERSCAN_CHAIN_ID,
            "apikey": config.ETHERSCAN_API_KEY,
        }

        async with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < config.ETHERSCAN_REQUEST_DELAY_SEC:
                await asyncio.sleep(config.ETHERSCAN_REQUEST_DELAY_SEC - elapsed)

            client = await self._get_client()
            try:
                response = await client.get(config.ETHERSCAN_BASE_URL, params=query)
            except httpx.HTTPError as exc:
                raise EtherscanError(f"Network error talking to Etherscan: {exc}") from exc
            finally:
                self._last_request_at = time.monotonic()
            self.api_calls += 1

        if response.status_code != 200:
            raise EtherscanError(f"Etherscan returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise EtherscanError("Etherscan returned a non-JSON response") from exc

        status = str(payload.get("status", ""))
        result = payload.get("result")

        if status == "1":
            return result

        message = str(payload.get("message", ""))
        if "no transactions found" in message.lower():
            return []

        # Rate-limit trips and dead/deprecated endpoints both land here. The
        # result field usually carries the human-readable reason.
        raise EtherscanError(f"Etherscan error: {message or 'unknown'} / {result}")

    async def get_outgoing_transfers(self, address: str) -> list[Transfer]:
        """
        Every ETH transfer SENT BY `address`, most recent first.

        WHY only outgoing: a trace follows the money forward. Transactions where
        this wallet is the recipient tell us where the funds came from, which is
        backwards from what the investigator needs here.

        SCOPE: external ETH transactions only (Etherscan's `txlist`). Internal
        transactions (contract-driven moves) and ERC-20 token transfers such as
        USDT are not covered yet - see the scope note in tracer.py.
        """
        key = normalize_address(address)
        if key in self._cache:
            self.cache_hits += 1
            return self._cache[key]

        raw = await self._request(
            {
                "module": "account",
                "action": "txlist",
                "address": key,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": config.MAX_TXNS_PER_ADDRESS,
                "sort": "desc",
            }
        )

        transfers: list[Transfer] = []
        if isinstance(raw, list):
            for tx in raw:
                transfer = _parse_transfer(tx, sender=key)
                if transfer is not None:
                    transfers.append(transfer)

        self._cache[key] = transfers
        return transfers


def _parse_transfer(tx: dict, sender: str) -> Transfer | None:
    """
    Turn one raw Etherscan row into a Transfer, or None if it is not usable.

    Dropped rows: failed transactions (they moved nothing), contract creations
    (no recipient), self-sends, incoming transactions, and zero-value calls.
    A zero-value row is normally a contract interaction - including an ERC-20
    transfer, whose real value lives in the token contract rather than in the
    ETH value field - so it carries no ETH flow for us to follow.
    """
    try:
        to_addr = (tx.get("to") or "").strip().lower()
        from_addr = (tx.get("from") or "").strip().lower()
        if not to_addr or from_addr != sender or to_addr == sender:
            return None

        # isError is "1" on a reverted transaction; no value actually moved.
        if str(tx.get("isError", "0")) == "1":
            return None

        value_eth = int(tx.get("value", "0")) / 1e18
        if value_eth <= 0:
            return None

        return Transfer(
            hash=tx.get("hash", ""),
            from_addr=from_addr,
            to_addr=to_addr,
            value_eth=value_eth,
            timestamp=int(tx.get("timeStamp", "0")),
            block=int(tx.get("blockNumber", "0")),
        )
    except (TypeError, ValueError):
        # A malformed row should skip, not kill an entire investigation.
        return None


# Process-wide singleton so the cache is shared across requests and traces.
_client = EtherscanClient()


def get_client() -> EtherscanClient:
    """The shared client. Always go through this so the cache is actually shared."""
    return _client
