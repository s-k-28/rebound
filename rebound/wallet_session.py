"""Server-side browser-wallet sessions for one ready transfer.

A session binds one wallet account to one original burn. The server prepares,
fee-caps and stores the exact transaction; the Flet UI reviews and approves it;
the wallet adapter page only hands that stored transaction to the wallet and
reports the hash. Delivery is shown only after RecoveryService.inspect_transaction
verifies that exact hash against the original transfer (canonical Arc block and the
transfer's own MessageReceived nonce and body).

State machine (all transitions are validated here):

    created -> connected -> reviewed -> approving -> approved -> sending
    sending -> submitted -> confirmed | failed (reverted) | unrelated
    sending -> reviewed (wallet rejected: nothing sent) | failed (adapter refused) | unknown

Reservations are per transfer and per process: while one session is approving,
approved, sending, submitted, unknown or unrelated, no other session for that
transfer can be approved. They are held in memory, so they cover every tab and
browser served by this one Rebound process, not other processes or a restart.
"""
import asyncio
import re
import secrets
import time
import weakref
from dataclasses import dataclass, field

from rebound import protocol as p
from rebound.cli import integer, validate_prepared
from rebound.clients import UpstreamError, quantity

ARC_CHAIN_ID = 5042002
ARC_CHAIN_HEX = hex(ARC_CHAIN_ID)
SESSION_TTL_SECONDS = 15 * 60
PICKUP_SECONDS = 90
MAX_SESSIONS = 64
# Same default as `rebound.cli recover --max-gas-usdc`: 0.1 testnet USDC (18 decimals).
MAX_NETWORK_FEE_WEI = 10**17
MAX_GAS_LIMIT = 2_400_000  # The service bound (2,000,000) plus its 20% margin.
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{22}", re.ASCII)
HOLDING = frozenset({"approving", "approved", "sending", "submitted", "unknown", "unrelated"})
FINAL = frozenset({"confirmed", "failed"})
WALLET_FIELDS = ("from", "to", "data", "value", "gas", "maxFeePerGas", "maxPriorityFeePerGas", "chainId")


class SessionNotFound(LookupError):
    pass


class SessionConflict(ValueError):
    """The request is valid but not allowed in the session's current state."""


@dataclass
class WalletSession:
    id: str
    tx_hash: str
    log_index: int
    created: float
    state: str = "created"
    account: str | None = None
    chain_id: str | None = None
    review: dict | None = None
    transaction: dict | None = None
    raw_message: str | None = None
    nonce: str | None = None
    reviewing: bool = False
    approved_at: float | None = None
    hash: str | None = None
    receipt: dict | None = None
    error: str = ""
    notice: str = ""
    version: int = 0
    _generation: int = field(default=0, repr=False)

    @property
    def transfer(self) -> str:
        return f"{self.tx_hash}:{self.log_index}"

    def view(self) -> dict:
        """The full snapshot for the in-process Flet UI."""
        return {"id": self.id, "state": self.state, "account": self.account, "chain_id": self.chain_id,
                "review": self.review, "transaction": self.transaction, "reviewing": self.reviewing,
                "hash": self.hash, "receipt": self.receipt, "error": self.error, "notice": self.notice,
                "version": self.version, "tx_hash": self.tx_hash, "log_index": self.log_index}

    def adapter_view(self) -> dict:
        """What the wallet adapter page may poll. It receives the transaction only by claiming it."""
        return {"state": self.state, "account": self.account, "chain_id": self.chain_id}


def wallet_fee_caps(gas_limit: int, base_fee: int, priority_fee: int) -> tuple[int, int]:
    """EIP-1559 caps sent with the transaction; the displayed maximum is gas x maxFeePerGas."""
    if gas_limit <= 0 or gas_limit > MAX_GAS_LIMIT:
        raise ValueError("Destination gas limit is outside the allowed recovery bound.")
    if base_fee <= 0 or priority_fee < 0:
        raise UpstreamError("Arc returned an invalid fee quote.")
    max_fee = 2 * base_fee + priority_fee
    if gas_limit * max_fee > MAX_NETWORK_FEE_WEI:
        raise ValueError("Arc's current fees would exceed Rebound's 0.1 testnet USDC cap for this transaction.")
    return max_fee, priority_fee


class WalletSessions:
    def __init__(self, service, *, clock=time.monotonic, ttl: float = SESSION_TTL_SECONDS,
                 pickup: float = PICKUP_SECONDS, limit: int = MAX_SESSIONS):
        self.service = service
        self.clock = clock
        self.ttl = ttl
        self.pickup = pickup
        self.limit = limit
        self._sessions: dict[str, WalletSession] = {}
        self._holders: dict[str, str] = {}

    # Lifecycle -----------------------------------------------------------------

    def create(self, tx_hash: str, log_index: int) -> WalletSession:
        tx_hash = p.hash32(tx_hash)
        if type(log_index) is not int or not 0 <= log_index <= 100_000:
            raise ValueError("Event log index must be an integer from 0 to 100000.")
        self._purge()
        if len(self._sessions) >= self.limit:
            raise SessionConflict("Too many wallet sessions are open on this server. Try again in a few minutes.")
        session_id = secrets.token_urlsafe(16)
        session = WalletSession(session_id, tx_hash, log_index, self.clock())
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> WalletSession:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise SessionNotFound("Unknown wallet session.")
        self._purge()
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound("This wallet session has expired. Start again from the transfer.")
        self._lapse_pickup(session)
        return session

    def close(self, session_id: str) -> None:
        """Forget a session the UI closed, unless it holds its transfer's reservation."""
        session = self._sessions.get(session_id)
        if session is not None and session.state not in HOLDING:
            del self._sessions[session_id]

    def holder(self, tx_hash: str, log_index: int) -> WalletSession | None:
        session_id = self._holders.get(f"{p.hash32(tx_hash)}:{log_index}")
        return self._sessions.get(session_id) if session_id else None

    def _purge(self) -> None:
        now = self.clock()
        for session_id, session in list(self._sessions.items()):
            if session.state not in HOLDING and now - session.created > self.ttl:
                del self._sessions[session_id]

    def _change(self, session: WalletSession, state: str, *, error: str = "", notice: str = "") -> None:
        session.state, session.error, session.notice = state, error, notice
        if state in HOLDING:
            self._holders[session.transfer] = session.id
        elif self._holders.get(session.transfer) == session.id:
            del self._holders[session.transfer]
        session.version += 1

    def _require(self, session: WalletSession, *states: str) -> None:
        if session.state not in states:
            raise SessionConflict(f"This wallet session is {session.state}; that step is not available now.")

    def _lapse_pickup(self, session: WalletSession) -> None:
        # Until the adapter claims the transaction, nothing can have reached the wallet.
        if session.state == "approved" and self.clock() - (session.approved_at or 0) > self.pickup:
            self._change(session, "reviewed", notice=(
                f"The wallet tab did not pick up the request within {int(self.pickup)} seconds. Nothing was sent."))

    # Connect and review ----------------------------------------------------------

    async def connect(self, session_id: str, account: str, chain_id: str) -> WalletSession:
        session = self.get(session_id)
        self._require(session, "created", "connected", "reviewed")
        account = p.address(account)
        if not isinstance(chain_id, str) or chain_id.lower() != ARC_CHAIN_HEX:
            raise ValueError("Switch the wallet to Arc Testnet (chain 5042002) before connecting.")
        session.account, session.chain_id = account, ARC_CHAIN_HEX
        session.review = session.transaction = session.raw_message = session.nonce = None
        self._change(session, "connected")
        await self.review(session_id)
        return session

    async def review(self, session_id: str) -> WalletSession:
        """Fresh revalidation and simulation from the connected caller, then exact fee caps."""
        session = self.get(session_id)
        self._require(session, "connected", "reviewed")
        session._generation += 1
        generation, account = session._generation, session.account
        session.reviewing = True
        session.version += 1
        try:
            review, transaction, raw, nonce = await self._prepare(session, account)
        except (ValueError, UpstreamError, TimeoutError) as exc:
            if session._generation == generation and session.state in {"connected", "reviewed"}:
                session.reviewing = False
                session.review = session.transaction = None
                self._change(session, "connected", error=_message(exc))
            return session
        if session._generation == generation and session.state in {"connected", "reviewed"}:
            session.reviewing = False
            session.review, session.transaction, session.raw_message, session.nonce = review, transaction, raw, nonce
            self._change(session, "reviewed")
        return session

    async def _prepare(self, session: WalletSession, account: str) -> tuple[dict, dict, str, str]:
        prepared = await self.service.prepare(session.tx_hash, session.log_index, account)
        tx, raw, message = validate_prepared(prepared, account, session.tx_hash, session.log_index)
        gas = integer(tx["gas"])
        block, priority, balance = await asyncio.gather(
            self.service.arc.call("eth_getBlockByNumber", ["latest", False]),
            self.service.arc.call("eth_maxPriorityFeePerGas"),
            self.service.arc.call("eth_getBalance", [account, "latest"]),
        )
        if not isinstance(block, dict) or block.get("baseFeePerGas") is None:
            raise UpstreamError("Arc did not report an EIP-1559 base fee.")
        max_fee, max_priority = wallet_fee_caps(gas, quantity(block["baseFeePerGas"]), quantity(priority))
        balance = quantity(balance)
        maximum = gas * max_fee
        transaction = {"from": account, "to": p.TRANSMITTER, "data": tx["data"].lower(), "value": "0x0",
                       "gas": hex(gas), "maxFeePerGas": hex(max_fee),
                       "maxPriorityFeePerGas": hex(max_priority), "chainId": ARC_CHAIN_HEX}
        review = {
            "recipient": message.mint_recipient, "amount": prepared["amount"], "fee": prepared["fee"],
            "net_amount": prepared["net_amount"], "nonce": message.nonce, "gas_limit": gas,
            "max_fee_per_gas": max_fee, "max_priority_fee_per_gas": max_priority,
            "max_network_fee_usdc": p.usdc(maximum, decimals=18),
            "caller_balance_usdc": p.usdc(balance, decimals=18),
            "shortfall_usdc": p.usdc(max(0, maximum - balance), decimals=18),
            "warning": prepared.get("warning", ""),
        }
        return review, transaction, raw, message.nonce

    # Approve, claim and report -------------------------------------------------

    async def approve(self, session_id: str) -> WalletSession:
        session = self.get(session_id)
        self._require(session, "reviewed")
        if session.reviewing or session.transaction is None or session.review is None:
            raise SessionConflict("Wait for the review to finish.")
        if session.review["shortfall_usdc"] != "0":
            raise SessionConflict("The connected account cannot cover the maximum network fee yet.")
        held = self.holder(session.tx_hash, session.log_index)
        if held is not None and held is not session and held.state in {"submitted", "unrelated"}:
            await self.verify(held.id)  # A resolved earlier send releases the reservation.
            held = self.holder(session.tx_hash, session.log_index)
        self._require(session, "reviewed")
        if held is not None and held is not session:
            raise SessionConflict("Another wallet session is already finishing this transfer. "
                                  "Wait for its result before sending again.")
        # Reserve before the fresh check so no other session can approve meanwhile.
        self._change(session, "approving")
        stored = session.transaction
        try:
            prepared = await self.service.prepare(session.tx_hash, session.log_index, session.account)
            tx, _, _ = validate_prepared(prepared, session.account, session.tx_hash, session.log_index)
            if tx["data"].lower() != stored["data"]:
                raise ValueError("Transfer details changed after review. Check again before sending.")
            if integer(tx["gas"]) > integer(stored["gas"]):
                raise ValueError("Arc's gas estimate rose after review. Check again before sending.")
            balance = quantity(await self.service.arc.call("eth_getBalance", [session.account, "pending"]))
            if balance < integer(stored["gas"]) * integer(stored["maxFeePerGas"]):
                raise ValueError("The connected account no longer covers the maximum network fee.")
        except (ValueError, UpstreamError, TimeoutError) as exc:
            if session.state == "approving":
                self._change(session, "reviewed", error=_message(exc))
            return session
        if session.state == "approving":
            session.approved_at = self.clock()
            self._change(session, "approved")
        return session

    def claim(self, session_id: str) -> dict:
        """Hand the stored transaction to the adapter exactly once."""
        session = self.get(session_id)
        self._require(session, "approved")
        self._change(session, "sending")
        return dict(session.transaction)

    def report(self, session_id: str, outcome: str, *, tx_hash: str | None = None,
               reason: str = "", code: int | None = None) -> WalletSession:
        session = self.get(session_id)
        if outcome == "submitted":
            tx_hash = p.hash32(tx_hash)
            if session.state == "submitted" and session.hash == tx_hash:
                return session  # Idempotent retry from the adapter.
            self._require(session, "sending")
            session.hash = tx_hash
            self._change(session, "submitted")
        elif outcome == "rejected":
            self._require(session, "sending")
            self._change(session, "reviewed", notice="You rejected the request in your wallet. Nothing was sent.")
        elif outcome == "not_sent":
            self._require(session, "sending")
            detail = reason[:200] if isinstance(reason, str) else ""
            self._change(session, "failed", error=f"The wallet tab refused to send: {detail} Nothing was sent.")
        elif outcome == "unknown":
            self._require(session, "sending")
            suffix = f" (wallet error {code})" if isinstance(code, int) else ""
            self._change(session, "unknown", error=(
                f"The wallet did not confirm whether it sent the transaction{suffix}. Do not send again: "
                "check the wallet's activity for a transaction to Arc's MessageTransmitterV2."))
        else:
            raise ValueError("Unknown wallet outcome.")
        return session

    def record_hash(self, session_id: str, tx_hash: str) -> WalletSession:
        """The user found the sent transaction in wallet activity after an unknown outcome."""
        session = self.get(session_id)
        self._require(session, "sending", "unknown", "unrelated")
        session.hash = p.hash32(tx_hash)
        self._change(session, "submitted")
        return session

    def release(self, session_id: str) -> WalletSession:
        """The user checked the wallet and confirms nothing was sent; frees the transfer."""
        session = self.get(session_id)
        self._require(session, "sending", "unknown", "unrelated")
        self._change(session, "failed", error="Released after you confirmed the wallet sent nothing for this transfer.")
        return session

    # Verify --------------------------------------------------------------------

    async def verify(self, session_id: str) -> WalletSession:
        session = self.get(session_id)
        if session.state not in {"submitted", "unrelated"} or not session.hash:
            return session
        tx_hash = session.hash
        try:
            result = await self._check_hash(session, tx_hash)
        except (ValueError, UpstreamError, TimeoutError):
            session.notice = "Rebound could not check the receipt right now. It will try again."
            session.version += 1
            return session
        if session.hash != tx_hash or session.state not in {"submitted", "unrelated"}:
            return session
        status = result["status"]
        session.notice = ""
        if status == "confirmed":
            session.receipt = result
            self._change(session, "confirmed")
            try:
                await asyncio.to_thread(self.service.store.remember_mint, session.tx_hash, session.log_index,
                                        session.nonce, tx_hash)
            except Exception:
                session.notice = "The verified receipt could not be saved to the local inbox index."
        elif status == "reverted":
            self._change(session, "failed", error=(
                "The Arc transaction reverted, so it did not deliver this transfer. Gas may have been spent. "
                "Refresh the transfer before any new attempt."))
        elif status == "unrelated":
            self._change(session, "unrelated", error=(
                f"This hash does not prove delivery of this transfer: {result['reason']} "
                "Do not send again until you have checked the wallet's activity."))
        else:
            session.version += 1
        return session

    async def _check_hash(self, session: WalletSession, tx_hash: str) -> dict:
        """Bind a reported hash to this session, then let the service prove delivery.

        The receipt read only decides pending, reverted or not-from-this-session. Delivery
        proof is RecoveryService.inspect_transaction(destination_hash=...): the original
        burn and attestation are re-derived, the Arc nonce must be consumed, and the hash
        must pass the canonical-block and MessageReceived nonce/body check.
        """
        receipt = await self.service.arc.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return {"status": "pending"}
        try:
            if not isinstance(receipt, dict) or p.hash32(receipt.get("transactionHash")) != tx_hash:
                return {"status": "unrelated", "reason": "Arc returned a receipt for a different transaction."}
            if receipt.get("status") != "0x1":
                return {"status": "reverted"}
            if p.address(receipt.get("to")) != p.TRANSMITTER:
                return {"status": "unrelated", "reason": "it was not sent to Arc's MessageTransmitterV2."}
            if p.address(receipt.get("from")) != session.account:
                return {"status": "unrelated", "reason": "it was sent from a different account than the one reviewed."}
        except ValueError:
            return {"status": "unrelated", "reason": "the receipt is malformed."}
        inspected = await self.service.inspect_transaction(session.tx_hash, log_index=session.log_index,
                                                           destination_hash=tx_hash)
        rows = [row for row in inspected.get("transfers", []) if row.get("key") == session.transfer]
        if len(rows) != 1:
            raise ValueError("The original transfer could not be inspected.")
        row = rows[0]
        if not row.get("submitted_receipt_verified"):
            return {"status": "unrelated", "reason": (
                "Rebound's receipt check for this transfer did not accept it. Delivery needs a consumed Arc nonce, "
                "a canonical Arc block and a MessageReceived event with this transfer's nonce and message body.")}
        try:
            fee_paid = p.usdc(quantity(receipt.get("gasUsed")) * quantity(receipt.get("effectiveGasPrice")), decimals=18)
        except UpstreamError:
            fee_paid = None
        return {"status": "confirmed", "tx_hash": tx_hash, "block": quantity(receipt["blockNumber"]),
                "url": row["submitted_receipt_url"], "fee_paid_usdc": fee_paid, "nonce": row.get("nonce")}


def _message(exc: Exception) -> str:
    if isinstance(exc, (ValueError, UpstreamError)):
        return str(exc)
    return "Arc did not answer in time. Check again."


_managers: "weakref.WeakKeyDictionary[object, WalletSessions]" = weakref.WeakKeyDictionary()


def sessions_for(service) -> WalletSessions:
    """One session manager, and so one reservation table, per recovery service."""
    manager = _managers.get(service)
    if manager is None:
        manager = _managers[service] = WalletSessions(service)
    return manager
