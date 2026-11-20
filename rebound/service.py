"""Transfer discovery, reconciliation and unsigned transaction preparation."""

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from rebound import protocol as p
from rebound.clients import Circle, RangeLimitError, Rpc, UpstreamError, quantity
from rebound.store import CandidateStore, IndexedBurn

MIN_RECOVERY_CONFIRMATIONS = 12
BLOCK_TIME_CACHE_SIZE = 4096
TIME_SEARCH_TIMEOUT_SECONDS = 30


def _parse_utc_cutoff(value: str) -> tuple[int, str]:
    if (not isinstance(value, str) or len(value) > 20 or not re.fullmatch(
            # The hour is limited to 00-23 here. Python 3.14's fromisoformat reads 24:00 as midnight
            # of the next day, which would silently move the cutoff by a day.
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: (?:[01][0-9]|2[0-3]):[0-9]{2}"
            r"|T(?:[01][0-9]|2[0-3]):[0-9]{2}(?::[0-9]{2})?Z?)", value)):
        raise ValueError("Use a UTC cutoff as YYYY-MM-DD HH:MM or YYYY-MM-DDTHH:MM[:SS][Z].")
    try:
        cutoff = datetime.fromisoformat(value.removesuffix("Z")).replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("The UTC cutoff is not a valid date and time.") from None
    return int(cutoff.timestamp()), cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Settings:
    base_rpc: str = "https://sepolia.base.org"
    arc_rpc: str = "https://rpc.testnet.arc.network"
    circle_url: str = "https://iris-api-sandbox.circle.com"
    database: str = "data/rebound.sqlite3"
    stream_secret: str = ""
    stream_security_token: str = ""
    # Public address used by the QuickNode walkthrough. The default is the testnet
    # sample wallet documented in docs/LIVE-RECOVERY.md.
    quicknode_demo_wallet: str = "0x395809d8da4a70145d5060448070a15367df722c"
    # Off until a real wallet-extension run on a funded throwaway account passes.
    browser_wallet: bool = False

    def __post_init__(self):
        for endpoint in (self.base_rpc, self.arc_rpc, self.circle_url):
            parsed = urlsplit(endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("Upstream endpoints must be valid HTTP(S) URLs.")
        if self.quicknode_demo_wallet:
            p.address(self.quicknode_demo_wallet)

    @classmethod
    def from_env(cls):
        return cls(
            base_rpc=os.getenv("BASE_SEPOLIA_RPC", cls.base_rpc),
            arc_rpc=os.getenv("ARC_TESTNET_RPC", cls.arc_rpc),
            circle_url=os.getenv("CIRCLE_SANDBOX_URL", cls.circle_url),
            database=os.getenv("REBOUND_DB", cls.database),
            stream_secret=os.getenv("REBOUND_STREAM_SECRET", ""),
            stream_security_token=os.getenv("REBOUND_STREAM_SECURITY_TOKEN", ""),
            # An empty value (as in .env.example) means "use the default", not "no wallet".
            quicknode_demo_wallet=os.getenv("REBOUND_QUICKNODE_DEMO_WALLET") or cls.quicknode_demo_wallet,
            browser_wallet=os.getenv("REBOUND_BROWSER_WALLET") == "1",
        )

    @property
    def stream_enabled(self) -> bool:
        return bool(self.stream_security_token or self.stream_secret)


class SourceChanged(ValueError):
    pass


class AmbiguousBurn(ValueError):
    pass


class BusyError(UpstreamError):
    pass


@dataclass(frozen=True)
class DestinationState:
    status: str
    block: int
    nonce_used: bool


@dataclass(frozen=True)
class Attestation:
    message: p.Message
    raw: str
    signature: str
    receipt_hint: str | None
    forward_state: str | None


EXPLANATIONS = {
    "ready": "The original transfer has a matching attestation and its destination nonce is unused. Review it before preparing recovery.",
    "completed": "Arc has already consumed this message's nonce. Do not send or mint again.",
    "expired": "The attestation expired before delivery. Request re-attestation for the existing burn; do not send USDC again.",
    "caller_required": "This transfer permits only its original destination caller. Rebound cannot substitute a different caller.",
    "source_confirming": f"Wait for {MIN_RECOVERY_CONFIRMATIONS} Base confirmations before preparing recovery.",
    "awaiting_attestation": "The source burn is confirmed, but Circle's matching signed message is not available yet. Refresh later.",
    "unsupported": "The available message cannot be matched safely to this supported transfer. No recovery transaction is prepared.",
    "source_changed": "The indexed burn no longer matches the canonical source chain. Refresh and investigate the original receipt.",
    "unavailable": "A required provider could not verify the transfer. Refresh later; this does not establish that funds are lost.",
    "managed_delivery": "This burn requested Circle forwarding. Arc has not consumed the nonce. Check the forwarding status in the original app.",
}


async def _gather_reads(*coroutines):
    """Cancel and settle sibling reads before propagating a failed range."""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@dataclass(frozen=True)
class ArcGasQuote:
    price: int
    gas_limit: int
    balance: int

    @property
    def maximum_cost(self) -> int:
        return self.price * self.gas_limit

    @property
    def shortfall(self) -> int:
        return max(0, self.maximum_cost - self.balance)


def _remember_block_time(times: dict[str, int] | None, block: dict) -> None:
    """Keep a canonical block's timestamp, keyed by the block hash that commits to it."""
    if times is None:
        return
    try:
        block_hash = p.hash32(block.get("hash"))
        timestamp = quantity(block.get("timestamp"))
        # This optional display value must be representable on this platform.
        # Invalid metadata must never change a transfer's proof or control state.
        datetime.fromtimestamp(timestamp, UTC)
        times[block_hash] = timestamp
    except (ValueError, OverflowError, OSError, UpstreamError):
        return
    while len(times) > BLOCK_TIME_CACHE_SIZE:
        times.pop(next(iter(times)))


async def arc_gas_quote(rpc: Rpc, caller: str, gas_limit: int, *, block: str = "latest") -> ArcGasQuote:
    price, balance = await _gather_reads(
        rpc.call("eth_gasPrice"), rpc.call("eth_getBalance", [caller, block]),
    )
    price = quantity(price)
    if price <= 0:
        raise UpstreamError("Arc returned an invalid gas price.")
    return ArcGasQuote(price, gas_limit, quantity(balance))


async def verify_arc_mint_receipt(rpc: Rpc, receipt: object, tx_hash: str, raw: str, message: p.Message,
                                  *, block_times: dict[str, int] | None = None) -> bool:
    """Match a mint to its canonical Arc block and original attested message."""
    if not isinstance(receipt, dict) or not p.matches_mint_receipt(receipt, tx_hash, raw, message):
        return False
    try:
        block_number = quantity(receipt.get("blockNumber"))
        block_hash = p.hash32(receipt.get("blockHash"))
        block = await rpc.call("eth_getBlockByNumber", [hex(block_number), False])
        if (not isinstance(block, dict) or p.hash32(block.get("hash")) != block_hash
                or quantity(block.get("number")) != block_number):
            return False
        _remember_block_time(block_times, block)
        transaction_index = quantity(receipt["transactionIndex"]) if "transactionIndex" in receipt else None
        transactions = block.get("transactions")
        if not isinstance(transactions, list):
            return False
        hashes = [p.hash32(value) for value in transactions]
        if transaction_index is None:
            if tx_hash not in hashes:
                return False
        elif transaction_index >= len(hashes) or hashes[transaction_index] != tx_hash:
            return False
    except (ValueError, UpstreamError):
        return False

    # Some providers omit redundant log fields. If present, they must agree with the receipt.
    linked_logs = []
    for log in receipt["logs"]:
        if not isinstance(log, dict):
            continue
        try:
            if any(field in log and p.hash32(log[field]) != expected for field, expected in (
                ("transactionHash", tx_hash), ("blockHash", block_hash),
            )):
                continue
            if "blockNumber" in log and quantity(log["blockNumber"]) != block_number:
                continue
            if "transactionIndex" in log and quantity(log["transactionIndex"]) != transaction_index:
                continue
        except (ValueError, UpstreamError):
            continue
        linked_logs.append(log)
    return p.matches_mint_receipt(receipt | {"logs": linked_logs}, tx_hash, raw, message)


class RecoveryService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.base = Rpc(settings.base_rpc, "Base Sepolia RPC", client)
        self.arc = Rpc(settings.arc_rpc, "Arc Testnet RPC", client)
        self.circle = Circle(settings.circle_url, client)
        self.store = CandidateStore(settings.database)
        self._scan_slots = asyncio.Semaphore(2)
        self._read_slots = asyncio.Semaphore(3)
        self._admitted = 0
        self._admission_limit = 8
        self._log_chunk_size = 1000
        self._readiness_lock = asyncio.Lock()
        self._readiness_cache: dict | None = None
        self._readiness_expires = 0.0
        self._block_times: dict[str, int] = {}

    async def _verify_chain(self, rpc: Rpc, chain: int, contract: str):
        chain_id, code = await _gather_reads(
            rpc.call("eth_chainId"), rpc.call("eth_getCode", [contract, "latest"]),
        )
        if quantity(chain_id) != chain:
            raise UpstreamError(f"{rpc.label} is connected to the wrong network. Check configuration.")
        try:
            present = bool(p.hex_bytes(code))
        except ValueError as exc:
            raise UpstreamError(f"{rpc.label} could not verify the required contract.") from exc
        if not present:
            raise UpstreamError(f"{rpc.label} has no deployed contract at the required address.")

    @asynccontextmanager
    async def _admit(self):
        if self._admitted >= self._admission_limit:
            raise BusyError("Rebound is checking other transfers. Try again in a few seconds.")
        self._admitted += 1
        try:
            async with asyncio.timeout(120), self._scan_slots:
                yield
        finally:
            self._admitted -= 1

    async def readiness(self) -> dict:
        async def probe(check) -> str:
            try:
                async with asyncio.timeout(7):
                    await check
            except (UpstreamError, TimeoutError):
                return "unavailable"
            return "ok"

        async with self._readiness_lock:
            if self._readiness_cache is not None and time.monotonic() < self._readiness_expires:
                return self._readiness_cache
            base, circle, arc = await _gather_reads(
                probe(self._verify_chain(self.base, 84532, p.MESSENGER)),
                probe(self.circle.check_route()),
                probe(self._verify_chain(self.arc, 5042002, p.TRANSMITTER)),
            )
            providers = {"base_sepolia": base, "circle_cctp_fee_route": circle, "arc_testnet": arc}
            ready = all(status == "ok" for status in providers.values())
            self._readiness_cache = {
                "status": "ready" if ready else "degraded",
                "providers": providers,
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "stream_ingestion_configured": self.settings.stream_enabled,
            }
            self._readiness_expires = time.monotonic() + (30 if ready else 5)
            return self._readiness_cache

    def ingest_logs(self, logs: list[dict], *, signed: bool = False) -> int:
        if not isinstance(logs, list) or len(logs) > 5000:
            raise ValueError("Provide at most 5,000 event logs per ingestion request.")
        candidates = []
        for log in logs:
            try:
                burn = p.decode_burn(log)
                if burn.destination_domain != 26:
                    continue
                candidates.append((burn, log))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        self.store.save_many(candidates, from_stream=True, signed=signed)
        return len(candidates)

    async def _block_at_time(self, timestamp: int, latest: int) -> tuple[int, int]:
        """Return the last block at the cutoff and this provider's readable floor."""
        if not 0 <= latest < 2**64:
            raise UpstreamError("Base Sepolia returned an invalid source head for time lookup.")
        seen: dict[int, tuple[int, str]] = {}

        async def header(tag: str, expected: int | None = None) -> tuple[int, int]:
            try:
                # One sequential read per admitted scan, like the chain check. Taking a read
                # slot would queue this deadline behind another scan's bulk log reads.
                block = await self.base.call("eth_getBlockByNumber", [tag, False])
                if not isinstance(block, dict):
                    raise ValueError
                for field in ("number", "timestamp"):
                    if (not isinstance(block.get(field), str) or len(block[field]) > 18
                            or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})", block[field])):
                        raise ValueError
                number, seconds = quantity(block["number"]), quantity(block["timestamp"])
                block_hash = p.hash32(block.get("hash"))
                if (number > latest or (expected is not None and number != expected)
                        or seconds > 253_402_300_799):
                    raise ValueError
                identity = (seconds, block_hash)
                if number in seen and seen[number] != identity:
                    raise ValueError
                seen[number] = identity
                return number, seconds
            except (ValueError, TypeError, UpstreamError) as exc:
                raise UpstreamError("Base Sepolia could not verify the requested time range. Its available source history may be limited; try another RPC or a more recent UTC cutoff.") from exc

        _, head_time = await header(hex(latest), latest)
        # 'earliest' is the readable retention floor on pruned RPCs, not always genesis.
        floor, floor_time = await header("earliest")
        if floor_time > head_time:
            raise UpstreamError("Base Sepolia returned inconsistent block timestamps.")
        if timestamp < floor_time:
            first = datetime.fromtimestamp(floor_time, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            raise ValueError(f"The cutoff is before available source history, which starts at {first} (block {floor:,}).")
        low, high, low_time, high_time = floor, latest, floor_time, head_time
        if timestamp >= head_time:
            low = latest
        else:
            # Upper-bound search: equal timestamps always move right, including every tie.
            for _ in range(64):
                if high - low <= 1:
                    break
                middle = (low + high) // 2
                _, middle_time = await header(hex(middle), middle)
                if not low_time <= middle_time <= high_time:
                    raise UpstreamError("Base Sepolia returned inconsistent block timestamps.")
                if middle_time <= timestamp:
                    low, low_time = middle, middle_time
                else:
                    high, high_time = middle, middle_time
            if high - low > 1:
                raise UpstreamError("Base Sepolia time lookup exceeded its bounded search.")
        # Re-read the final bracket so a changed boundary cannot silently move the cutoff.
        _, selected_time = await header(hex(low), low)
        if selected_time > timestamp:
            raise UpstreamError("Base Sepolia could not establish the UTC cutoff.")
        if low < latest:
            _, following_time = await header(hex(low + 1), low + 1)
            if following_time <= timestamp:
                raise UpstreamError("Base Sepolia could not establish the latest block at the UTC cutoff.")
        return low, floor

    async def scan(self, wallet: str, blocks: int = 2000, *, end_block: int | None = None,
                   end_time: str | None = None) -> dict:
        wallet = p.address(wallet)
        if not isinstance(blocks, int) or isinstance(blocks, bool) or not 1 <= blocks <= 50_000:
            raise ValueError("History must contain between 1 and 50,000 source blocks.")
        if end_block is not None and (type(end_block) is not int or end_block < 0):
            raise ValueError("End block must be a non-negative block number.")
        if end_block is not None and end_time is not None:
            raise ValueError("Choose either an end block or a UTC end time, not both.")
        cutoff = _parse_utc_cutoff(end_time) if end_time is not None else None
        async with self._admit():
            await self._verify_chain(self.base, 84532, p.MESSENGER)
            latest = quantity(await self.base.call("eth_blockNumber"))
            if end_block is not None and end_block > latest:
                raise ValueError(f"End block must be at or before Base Sepolia block {latest:,}.")
            end = latest if end_block is None else end_block
            floor = 0
            if cutoff is not None:
                try:
                    async with asyncio.timeout(TIME_SEARCH_TIMEOUT_SECONDS):
                        end, floor = await self._block_at_time(cutoff[0], latest)
                except TimeoutError as exc:
                    raise UpstreamError("Base Sepolia UTC time lookup timed out. Try again or use an end block.") from exc
            start = max(floor, end - blocks + 1)
            source_token = "0x" + "00" * 12 + p.USDC[2:]

            async def read_chunk(first: int, last: int):
                try:
                    async with self._read_slots:
                        logs = await self.base.call("eth_getLogs", [{
                            "address": p.MESSENGER, "fromBlock": hex(first),
                            "toBlock": hex(last), "topics": [p.BURN_TOPIC, source_token],
                        }])
                except RangeLimitError:
                    if first == last:
                        raise
                    # Public Base RPC currently caps log requests at 200 blocks.
                    # Continue subdividing for a provider with an even tighter cap.
                    size = min(200, max(1, (last - first + 1) // 2))
                    self._log_chunk_size = min(self._log_chunk_size, size)
                    batches = await _gather_reads(*(read_chunk(a, min(a + size - 1, last)) for a in range(first, last + 1, size)))
                    return [burn for batch in batches for burn in batch]
                if not isinstance(logs, list):
                    raise UpstreamError("Base Sepolia RPC returned unreadable event history.")
                # Ignore unrelated wallets; this index is a candidate cache, not a ledger.
                matched = []
                for log in logs:
                    try:
                        burn = p.decode_burn(log)
                        if first <= burn.block_number <= last and burn.destination_domain == 26 and wallet in (burn.depositor, burn.mint_recipient):
                            matched.append((burn, log))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                return matched

            # Any failed chunk fails this scan rather than pretending history was complete.
            size = self._log_chunk_size
            batches = await _gather_reads(*(read_chunk(first, min(first + size - 1, end)) for first in range(start, end + 1, size)))
            await asyncio.to_thread(self.store.save_many, [entry for batch in batches for entry in batch])
            fixed = end_block is not None or cutoff is not None
            burns = self.store.candidates(wallet, block_range=(start, end) if fixed else None)
            transfers = await self._inspect_candidates(burns, latest)
            indexed = " (fixed range)" if fixed else ", plus previously indexed candidates"
            shortened = cutoff is not None and start > max(0, end - blocks + 1)
            cutoff_note = f" UTC cutoff {cutoff[1]} (latest block at or before)." if cutoff else ""
            if shortened:
                cutoff_note += " Range shortened to the provider's available source history."
            return {
                "wallet": wallet, "from_block": start, "to_block": end,
                "coverage": f"Base Sepolia blocks {start:,}–{end:,}{indexed}. Covers native USDC CCTP V2 burns to Arc.{cutoff_note}",
                "partial": len(burns) > 100 or shortened, "transfers": transfers,
            }

    async def scan_stream(self, wallet: str) -> dict:
        """Find a wallet's transfers from burns stored by the Stream endpoint.

        Reads the persisted Stream index only. There is no source ``eth_getLogs``
        history scan, so an empty result means nothing was delivered for this
        address, not that no transfer exists. After discovery each burn is
        rechecked against its source receipt, Circle's attestation and Arc.
        """
        wallet = p.address(wallet)
        if not self.settings.stream_enabled:
            raise ValueError("QuickNode Streams ingestion is not enabled on this server.")
        async with self._admit():
            burns = self.store.candidates(wallet, stream_only=True)
            transfers = []
            if burns:
                try:
                    await self._verify_chain(self.base, 84532, p.MESSENGER)
                    latest = quantity(await self.base.call("eth_blockNumber"))
                    transfers = await self._inspect_candidates(burns, latest)
                except UpstreamError as exc:
                    count = min(len(burns), 100)
                    raise UpstreamError(
                        f"The Stream index has {count} matching burn{'s' if count != 1 else ''} for this address, "
                        f"but Rebound could not finish verifying {'them' if count != 1 else 'it'}. {exc} Retry shortly."
                    ) from exc
            return {"wallet": wallet, "from_block": None, "to_block": None, "partial": len(burns) > 100,
                    "source": "stream_index", "delivered": len(burns),
                    "coverage": "Stream endpoint records for this address. Rebound did not scan Base Sepolia "
                                "history. Block coverage is unknown.",
                    "transfers": transfers}

    def stream_status(self, wallet: str) -> dict:
        """What the Stream index holds for a wallet. Reads the index only, no RPC.

        ``delivered`` counts indexed burns with an accepted HMAC. It measures
        endpoint authentication. ``local`` counts shared-secret adapter records.
        """
        wallet = p.address(wallet)
        summary = self.store.stream_summary(wallet) if self.settings.stream_enabled else {"signed": 0, "last_signed": None, "local": 0}
        return {"wallet": wallet, "enabled": self.settings.stream_enabled, "delivered": summary["signed"],
                "last_received": summary["last_signed"], "local": summary["local"]}

    async def _inspect_candidates(self, burns: list[IndexedBurn], source_head: int) -> list[dict]:
        async def inspect(candidate: IndexedBurn):
            async with self._read_slots:
                row = await self.diagnose(candidate.burn, source_head=source_head)
                row["discovery_source"] = "stream" if candidate.from_stream else "rpc"
                if candidate.from_stream:
                    row["stream_received_at"] = candidate.received_at
                    row["stream_signed"] = candidate.signed
                return row

        return await _gather_reads(*(inspect(candidate) for candidate in burns[:100]))

    async def inspect_transaction(self, tx_hash: str, *, log_index: int | None = None,
                                  destination_hash: str | None = None) -> dict:
        """Inspect source events and optionally verify one submitted Arc receipt.

        Submitted evidence is separate from Circle/local receipt discovery. It is
        never saved and cannot change the transfer's independently checked state.
        """
        tx_hash = p.hash32(tx_hash)
        if log_index is not None and (type(log_index) is not int or log_index < 0):
            raise ValueError("log_index must identify one non-negative source event index.")
        if destination_hash is not None:
            if log_index is None:
                raise ValueError("destination_hash requires an explicit log_index for the source event.")
            destination_hash = p.hash32(destination_hash)
        async with self._admit():
            await self._verify_chain(self.base, 84532, p.MESSENGER)
            receipt_burns = await self._receipt_burns(tx_hash)
            burns = [burn for burn in receipt_burns if burn.destination_domain == 26]
            if log_index is not None:
                burns = [burn for burn in burns if burn.log_index == log_index]
            latest = quantity(await self.base.call("eth_blockNumber")) if burns else None

            async def inspect(burn: p.Burn) -> dict:
                async with self._read_slots:
                    row = await self.diagnose(burn, source_head=latest, receipt_burns=receipt_burns,
                                              destination_hash=destination_hash)
                    row["discovery_source"] = "receipt"
                    return row

            transfers = await _gather_reads(*(inspect(burn) for burn in burns[:100]))
            return {"tx_hash": tx_hash, "coverage": "One Base Sepolia receipt. Covers native USDC CCTP V2 burns to Arc.",
                    "partial": len(burns) > 100, "transfers": transfers}

    async def _receipt_burns(self, tx_hash: str) -> list[p.Burn]:
        tx_hash = p.hash32(tx_hash)
        receipt = await self.base.call("eth_getTransactionReceipt", [tx_hash])

        def source_quantity(value) -> int:
            # Numeric parsing errors describe malformed evidence. Actual RPC
            # outages still propagate as UpstreamError, rather than a reorg.
            try:
                return quantity(value)
            except UpstreamError as exc:
                raise SourceChanged("The source receipt could not be matched safely.") from exc

        try:
            if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
                raise SourceChanged("Source burn receipt is missing or unsuccessful.")
            if p.hash32(receipt["transactionHash"]) != tx_hash:
                raise SourceChanged("Source receipt does not identify the expected transaction.")
            block_number = source_quantity(receipt["blockNumber"])
            block_hash = p.hash32(receipt["blockHash"])
            transaction_index = source_quantity(receipt["transactionIndex"])
            block = await self.base.call("eth_getBlockByNumber", [hex(block_number), False])
            if (not isinstance(block, dict) or p.hash32(block.get("hash")) != block_hash
                    or source_quantity(block.get("number")) != block_number):
                raise SourceChanged("Source burn is no longer in its canonical block.")
            transactions = block.get("transactions")
            if (not isinstance(transactions, list) or transaction_index >= len(transactions)
                    or p.hash32(transactions[transaction_index]) != tx_hash):
                raise SourceChanged("Source transaction is not at its canonical block position.")
            _remember_block_time(self._block_times, block)
            logs = receipt["logs"]
            if not isinstance(logs, list):
                raise ValueError("Invalid receipt logs.")
            burns = []
            for log in logs:
                if not isinstance(log, dict):
                    continue
                try:
                    candidate = p.decode_burn(log)
                except ValueError:
                    continue
                if candidate.tx_hash != tx_hash or candidate.block_number != block_number or candidate.block_hash != block_hash:
                    raise SourceChanged("Burn event identity does not match its receipt.")
                if "transactionIndex" in log and source_quantity(log["transactionIndex"]) != transaction_index:
                    raise SourceChanged("Burn event transaction position does not match its receipt.")
                burns.append(candidate)
            return burns
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            if isinstance(exc, SourceChanged):
                raise
            raise SourceChanged("The source receipt could not be matched safely.") from exc

    async def _receipt_burn(self, tx_hash: str, log_index: int, expected: p.Burn | None = None) -> p.Burn:
        return self._match_receipt_burn(await self._receipt_burns(tx_hash), log_index, expected)

    def _match_receipt_burn(self, burns: list[p.Burn], log_index: int, expected: p.Burn | None = None) -> p.Burn:
        matches = [burn for burn in burns if burn.log_index == log_index]
        if len(matches) != 1:
            raise SourceChanged("No unique supported burn remains at this event position.")
        burn = matches[0]
        if expected is not None and burn != expected:
            raise SourceChanged("The indexed burn differs from the current source receipt.")
        # Circle assigns the nonce offchain. Identical burns in one receipt cannot be
        # associated by economic fields alone and must not share an attestation.
        fields = ("amount", "depositor", "mint_recipient", "destination_domain", "caller", "max_fee", "min_finality", "hook")
        if sum(all(getattr(candidate, field) == getattr(burn, field) for field in fields) for candidate in burns) != 1:
            raise AmbiguousBurn("Source transaction contains ambiguous identical burns; exact message association is not supported yet.")
        return burn

    async def _matched_attestation(self, burn: p.Burn, *, inspect_forwarding: bool = False) -> Attestation | None:
        response = await self.circle.messages(burn.tx_hash)
        rows = response["messages"]
        if not rows:
            return None
        if p.hash32(response.get("sourceTxHash")) != burn.tx_hash:
            raise ValueError("Circle response does not identify the original source transaction.")
        matched = {}
        pending = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("status") != "complete":
                pending = True
                continue
            try:
                raw, attestation = row["message"], row["attestation"]
                message = p.decode_message(raw)
                p.validate_match(burn, message, inspect_forwarding=inspect_forwarding)
                p.receive_calldata(raw, attestation)  # Structural signature-size validation.
                receipt_hint = row.get("destinationMintTxHash") or row.get("forwardTxHash")
                forward_state = row.get("forwardState")
                if forward_state not in ("PENDING", "COMPLETE", "FAILED"):
                    forward_state = None
                matched[(raw.lower(), attestation.lower())] = Attestation(
                    message, raw, attestation, receipt_hint, forward_state,
                )
            except (ValueError, KeyError, TypeError):
                continue
        if len(matched) == 1:
            return next(iter(matched.values()))
        if not matched and pending:
            return None
        raise ValueError("No unique attestation matches the exact original burn.")

    async def _matching_arc_receipt(self, hint: str | None, attestation: Attestation) -> dict | None:
        if not hint:
            return None
        try:
            tx_hash = p.hash32(hint)
            receipt = await self.arc.call("eth_getTransactionReceipt", [tx_hash])
        except (ValueError, UpstreamError):
            return None
        verified = await verify_arc_mint_receipt(self.arc, receipt, tx_hash, attestation.raw, attestation.message,
                                                 block_times=self._block_times)
        return receipt if verified else None

    def _block_time(self, block_hash: str) -> str | None:
        timestamp = self._block_times.get(block_hash)
        if timestamp is None:
            return None
        try:
            return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            return None

    async def _verified_arc_receipt(self, burn: p.Burn, attestation: Attestation) -> dict | None:
        hint = attestation.receipt_hint
        receipt = await self._matching_arc_receipt(hint, attestation)
        if receipt is not None:
            return receipt
        local_hint = self.store.local_mint(burn, attestation.message.nonce)
        if local_hint and local_hint != hint:
            return await self._matching_arc_receipt(local_hint, attestation)
        return None

    async def _nonce_at(self, nonce: str, block: int) -> bool:
        value = quantity(await self.arc.call("eth_call", [
            {"to": p.TRANSMITTER, "data": p.used_nonce_calldata(nonce)}, hex(block),
        ]))
        if value not in (0, 1):
            raise UpstreamError("Arc returned an unrecognized nonce state.")
        return bool(value)

    async def _message_state(self, message: p.Message, caller: str | None = None) -> DestinationState:
        await self._verify_chain(self.arc, 5042002, p.TRANSMITTER)
        block = quantity(await self.arc.call("eth_blockNumber"))
        used = await self._nonce_at(message.nonce, block)
        return DestinationState(p.classify(message, nonce_used=used, destination_block=block, caller=caller),
                                block, used)

    async def replay_mint(self, tx_hash: str, log_index: int) -> dict:
        tx_hash = p.hash32(tx_hash)
        if type(log_index) is not int or log_index < 0:
            raise ValueError("Event log index must be a non-negative integer.")
        async with self._admit():
            await self._verify_chain(self.base, 84532, p.MESSENGER)
            burn = await self._receipt_burn(tx_hash, log_index)
            if burn.hook:
                raise ValueError("Replay supports direct mints without delivery hooks.")
            attestation = await self._matched_attestation(burn)
            if attestation is None:
                raise ValueError("Circle has not provided a matching attestation.")
            await self._verify_chain(self.arc, 5042002, p.TRANSMITTER)
            receipt = await self._verified_arc_receipt(burn, attestation)
            if receipt is None or str(receipt.get("to", "")).lower() != p.TRANSMITTER:
                raise ValueError("Replay requires a verified receipt for a direct Arc mint.")
            mint_block = quantity(receipt.get("blockNumber"))
            if mint_block < 1:
                raise ValueError("The receipt has no valid previous block for replay.")
            block = await self.arc.call("eth_getBlockByNumber", [hex(mint_block), False])
            if not isinstance(block, dict) or p.hash32(block.get("hash")) != p.hash32(receipt.get("blockHash")):
                raise ValueError("The mint receipt does not match its canonical Arc block.")
            caller = p.address(receipt.get("from"))
            message = attestation.message
            if message.caller not in (p.ZERO, caller):
                raise ValueError("The receipt caller differs from the attested caller.")
            previous = mint_block - 1
            try:
                before, after = await _gather_reads(
                    self._nonce_at(message.nonce, previous), self._nonce_at(message.nonce, mint_block),
                )
            except UpstreamError as exc:
                raise UpstreamError("Arc could not supply the historical nonce state for this mint.") from exc
            if before or not after:
                raise ValueError("The historical nonce states do not match this mint receipt.")
            transaction = {"from": caller, "to": p.TRANSMITTER, "value": "0x0",
                           "data": p.receive_calldata(attestation.raw, attestation.signature)}
            try:
                simulation = quantity(await self.arc.call("eth_call", [transaction, hex(previous)]))
            except UpstreamError as exc:
                raise UpstreamError("Arc could not simulate the mint against the previous block.") from exc
            if simulation != 1:
                raise ValueError("The mint simulation did not succeed at the previous block.")
            return {"source_tx_hash": tx_hash, "log_index": log_index, "nonce": message.nonce,
                    "arc_tx_hash": p.hash32(receipt["transactionHash"]), "previous_block": previous,
                    "mint_block": mint_block, "nonce_used_before": before, "nonce_used_at_mint": after,
                    "receive_simulated": True}

    async def diagnose(self, burn: p.Burn, *, source_head: int | None = None, caller: str | None = None,
                       receipt_burns: list[p.Burn] | None = None,
                       destination_hash: str | None = None) -> dict:
        row = {
            "key": burn.key, "tx_hash": burn.tx_hash, "log_index": burn.log_index,
            "block_number": burn.block_number, "amount": p.usdc(burn.amount),
            "net_amount": None, "recipient": burn.mint_recipient, "depositor": burn.depositor,
            "caller": burn.caller, "nonce": None, "fee": None, "example": False,
            "source_url": f"https://sepolia.basescan.org/tx/{burn.tx_hash}",
            "destination_url": None,
            "source_verified": False, "attestation_matched": False,
            "source_confirmations": None,
            "destination_block": None, "nonce_used": None, "expiration_block": None,
            "delivery_mode": None, "forward_state": None,
            "burned_at": None, "minted_at": None,
        }
        if destination_hash is not None:
            row.update(submitted_destination_hash=destination_hash,
                       submitted_receipt_verified=False, submitted_receipt_url=None)
        try:
            if receipt_burns is None:
                await self._receipt_burn(burn.tx_hash, burn.log_index, burn)
            else:
                self._match_receipt_burn(receipt_burns, burn.log_index, burn)
            row["source_verified"] = True
            row["burned_at"] = self._block_time(burn.block_hash)
            if burn.destination_domain != 26:
                raise ValueError("Unsupported destination route.")
            if source_head is None:
                source_head = quantity(await self.base.call("eth_blockNumber"))
            confirmations = max(0, source_head - burn.block_number + 1)
            row["source_confirmations"] = confirmations
            source_confirming = confirmations < MIN_RECOVERY_CONFIRMATIONS
            delivery_mode = p.forwarding_hook_kind(burn.hook)
            row["delivery_mode"] = delivery_mode
            attested = await self._matched_attestation(burn, inspect_forwarding=bool(delivery_mode))
            if attested is None:
                status = "source_confirming" if source_confirming else "awaiting_attestation"
            else:
                message = attested.message
                row.update(nonce=message.nonce, fee=p.usdc(message.fee), net_amount=p.usdc(message.amount - message.fee),
                           attestation_matched=True, expiration_block=message.expires_at)
                if delivery_mode:
                    row["forward_state"] = attested.forward_state
                destination = await self._message_state(message, caller)
                row.update(destination_block=destination.block, nonce_used=destination.nonce_used)
                status = destination.status
                if status == "completed":
                    receipt = await self._verified_arc_receipt(burn, attested)
                    if receipt is not None:
                        row["destination_url"] = f"{p.ARC_EXPLORER}/tx/{p.hash32(receipt['transactionHash'])}"
                        row["minted_at"] = self._block_time(p.hash32(receipt["blockHash"]))
                    if destination_hash is not None:
                        submitted = receipt
                        if receipt is None or p.hash32(receipt["transactionHash"]) != destination_hash:
                            submitted = await self._matching_arc_receipt(destination_hash, attested)
                        if submitted is not None:
                            row.update(submitted_receipt_verified=True,
                                       submitted_receipt_url=f"{p.ARC_EXPLORER}/tx/{destination_hash}")
                            if row["minted_at"] is None:
                                row["minted_at"] = self._block_time(p.hash32(submitted["blockHash"]))
                elif delivery_mode:
                    status = "managed_delivery"
                elif status == "ready" and source_confirming:
                    status = "source_confirming"
            row.update(status=status, explanation=EXPLANATIONS[status])
        except SourceChanged:
            row.update(status="source_changed", explanation=EXPLANATIONS["source_changed"])
        except UpstreamError:
            row.update(status="unavailable", explanation=EXPLANATIONS["unavailable"])
        except ValueError:
            row.update(status="unsupported", explanation=EXPLANATIONS["unsupported"])
        row["checked_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return row

    async def prepare(self, tx_hash: str, log_index: int, caller: str) -> dict:
        tx_hash, caller = p.hash32(tx_hash), p.address(caller)
        if not isinstance(log_index, int) or isinstance(log_index, bool) or log_index < 0:
            raise ValueError("Event log index must be a non-negative integer.")
        async with self._admit():
            return await self._prepare(tx_hash, log_index, caller)

    async def _prepare(self, tx_hash: str, log_index: int, caller: str) -> dict:
        await self._verify_chain(self.base, 84532, p.MESSENGER)
        burn = await self._receipt_burn(tx_hash, log_index)
        if burn.hook:
            raise ValueError("This burn contains a delivery hook. Check its status with the original app.")
        head = quantity(await self.base.call("eth_blockNumber"))
        if head - burn.block_number + 1 < MIN_RECOVERY_CONFIRMATIONS:
            raise ValueError("Transfer is source_confirming. Wait for more source confirmations.")
        attested = await self._matched_attestation(burn)
        if attested is None:
            raise ValueError("Transfer is awaiting_attestation. Refresh later.")
        message, raw, attestation = attested.message, attested.raw, attested.signature
        status = (await self._message_state(message, caller)).status
        if status != "ready":
            raise ValueError(f"Transfer is {status}. {EXPLANATIONS[status]}")
        transaction = {
            "from": caller, "to": p.TRANSMITTER, "data": p.receive_calldata(raw, attestation),
            "value": "0x0", "chainId": 5042002,
        }
        simulation = await self.arc.call("eth_call", [transaction, "latest"])
        if quantity(simulation) != 1:
            raise ValueError("Destination simulation did not confirm successful receipt. No transaction prepared.")
        gas = quantity(await self.arc.call("eth_estimateGas", [transaction]))
        if gas <= 0 or gas > 2_000_000:
            raise ValueError("Destination gas estimate is outside the allowed recovery bound.")
        gas_limit = (gas * 12 + 9) // 10
        transaction["gas"] = hex(gas_limit)
        quote = await arc_gas_quote(self.arc, caller, gas_limit)
        return {
            "transaction": transaction, "recipient": message.mint_recipient,
            "amount": p.usdc(message.amount), "net_amount": p.usdc(message.amount - message.fee),
            "fee": p.usdc(message.fee), "tx_hash": tx_hash, "log_index": log_index,
            "nonce": message.nonce,
            "gas_quote": {"max_cost_usdc": p.usdc(quote.maximum_cost, decimals=18),
                          "caller_balance_usdc": p.usdc(quote.balance, decimals=18),
                          "shortfall_usdc": p.usdc(quote.shortfall, decimals=18)},
            "warning": "Arc Testnet only. This completes the original transfer to its fixed recipient. Simulation is not a guarantee; competing attempts may still consume gas.",
        }
