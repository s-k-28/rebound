"""Demo lab: create one controlled, interrupted CCTP V2 transfer on Base Sepolia.

This is NOT part of the recovery product. Rebound itself never burns USDC. This
tool exists only to produce the release gate's controlled source burn: a small
manual Base Sepolia -> Arc Testnet ``depositForBurn`` to an owned recipient with a
zero destinationCaller and no hook, which then stops before any destination mint.
It never calls ``receiveMessage`` and never touches Rebound's SQLite store.

Call shapes: Circle TokenMessengerV2.sol (src/v2, Apache-2.0).
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from rebound.clients import Rpc, UpstreamError, quantity
from rebound.protocol import (
    BURN_TOPIC,
    MESSENGER,
    TRANSMITTER,
    USDC,
    ZERO,
    Burn,
    address,
    decode_burn,
    hash32,
    hex_bytes,
    usdc,
)
from rebound.terminal import read_hidden_key

BASE_CHAIN_ID = 84532
ARC_TESTNET_ID = 5042002
SOURCE_DOMAIN = 6
ARC_DOMAIN = 26
FINALITY = {"fast": 1000, "standard": 2000}
# Circle's published Base averages (CCTP "Finality and block confirmations").
EXPECTED_WAIT = {"fast": "about 8 seconds", "standard": "about 15-19 minutes"}
MAX_AMOUNT = 5_000_000  # 5 USDC: a demo burn never needs more.
MAX_FEE_CAP = 10_000  # 0.01 USDC: refuse any quote that implies a larger maxFee.
FEE_BUFFER = Decimal("1.2")  # Circle's fee guide suggests a 10-20% buffer.
APPROVE_GAS_BUDGET = 100_000
BURN_GAS_BUDGET = 300_000
EXECUTION_COST_CAP = 10**15  # 0.001 ETH per transaction, excluding the separate L1 data fee.
RECEIPT_POLLS, POLL_SECONDS = 30, 2
RECEIPT_TIMEOUT_SECONDS = 60
APPROVE_SELECTOR = keccak(text="approve(address,uint256)")[:4]
# The plain V2 burn (0x8e0250ee), not depositForBurnWithHook; tests pin the selector.
DEPOSIT_SELECTOR = keccak(
    text="depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"
)[:4]
BALANCE_OF = keccak(text="balanceOf(address)")[:4]
ALLOWANCE = keccak(text="allowance(address,address)")[:4]
CIRCLE_FAUCET = "https://faucet.circle.com"
ETH_FAUCETS = "https://docs.base.org/get-started/get-funds"
BASESCAN = "https://sepolia.basescan.org"
# Reverts that prove the call reached the token transfer, past every messenger check.
ROUTE_OK_REVERTS = ("transfer amount exceeds allowance", "transfer amount exceeds balance")


class LabRpc(Rpc):
    """Adds the EIP-1559 tip read and a simulation that keeps the revert reason."""

    METHODS = Rpc.METHODS | {"eth_maxPriorityFeePerGas"}

    async def simulate(self, tx: dict) -> tuple[bool, str]:
        try:
            response = await self.client.post(self.url, json={
                "jsonrpc": "2.0", "id": next(self._ids), "method": "eth_call",
                "params": [tx, "latest"],
            }, timeout=20)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise UpstreamError(f"{self.label} is unavailable. Retry or use --rpc.") from exc
        if not isinstance(data, dict):
            raise UpstreamError(f"{self.label} returned an unreadable simulation.")
        if isinstance(data.get("error"), dict):
            reason = re.sub(r"[^ -~]", "", str(data["error"].get("message", "")))[:160]
            return False, reason or "execution reverted"
        result = data.get("result")
        try:
            hex_bytes(result)
        except ValueError as exc:
            raise UpstreamError(f"{self.label} returned an unreadable simulation.") from exc
        return True, result


@dataclass(frozen=True)
class Plan:
    amount: int
    recipient: str
    speed: str
    threshold: int
    fee_bps: Decimal
    max_fee: int

    @property
    def approve_data(self) -> str:
        return approve_calldata(self.amount)

    @property
    def burn_data(self) -> str:
        return burn_calldata(self.amount, self.recipient, self.max_fee, self.threshold)


def word(value: str) -> bytes:
    return bytes.fromhex(address(value)[2:]).rjust(32, b"\0")


def approve_calldata(amount: int) -> str:
    """Exact-amount approval for TokenMessengerV2; never unlimited."""
    return "0x" + (APPROVE_SELECTOR + encode(["address", "uint256"], [MESSENGER, amount])).hex()


def burn_calldata(amount: int, recipient: str, max_fee: int, threshold: int) -> str:
    """Plain depositForBurn: destination domain 26, zero destinationCaller, no hook."""
    return "0x" + (DEPOSIT_SELECTOR + encode(
        ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
        [amount, ARC_DOMAIN, word(recipient), USDC, b"\0" * 32, max_fee, threshold],
    )).hex()


def parse_amount(text: str) -> int:
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError("--amount must be a decimal USDC amount, such as 1 or 0.5.") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("--amount must be positive with at most 6 decimal places.")
    if value > Decimal(MAX_AMOUNT).scaleb(-6):
        raise ValueError(f"The lab caps a controlled burn at {usdc(MAX_AMOUNT)} USDC.")
    if value != value.quantize(Decimal("0.000001")):
        raise ValueError("--amount must be positive with at most 6 decimal places.")
    return int(value * 10**6)


def parse_recipient(value: str) -> str:
    recipient = address(value)
    if recipient in (ZERO, MESSENGER, TRANSMITTER, USDC):
        raise ValueError("Use an Arc Testnet wallet you control as the recipient, not a contract.")
    return recipient


async def verify_recipient(arc: Rpc, recipient: str) -> None:
    if quantity(await arc.call("eth_chainId")) != ARC_TESTNET_ID:
        raise ValueError("The destination RPC is not Arc Testnet. Nothing was sent.")
    if hex_bytes(await arc.call("eth_getCode", [recipient, "latest"])):
        raise ValueError("The recipient already has contract code on Arc Testnet. Use a wallet you control.")


def max_fee_from_quote(rows: object, threshold: int, amount: int) -> tuple[Decimal, int]:
    """Circle quotes minimumFee in basis points; add the guide's buffer and round up."""
    if not isinstance(rows, list):
        raise UpstreamError("Circle returned an unreadable fee quote.")
    for row in rows:
        if not isinstance(row, dict) or row.get("finalityThreshold") != threshold:
            continue
        bps = row.get("minimumFee")
        if type(bps) not in (int, float) or not 0 <= bps <= 10_000:
            raise UpstreamError("Circle returned an unreadable fee quote.")
        bps = Decimal(str(bps))
        fee = int((Decimal(amount) * bps * FEE_BUFFER / 10_000).to_integral_value(ROUND_CEILING))
        if fee > MAX_FEE_CAP or fee >= amount:
            raise ValueError(
                f"Circle's quote ({bps} bps) implies maxFee {usdc(fee)} USDC, above the lab's "
                f"{usdc(MAX_FEE_CAP)} USDC bound or not below the amount. Nothing was sent."
            )
        return bps, fee
    raise UpstreamError(f"Circle returned no fee quote for finality threshold {threshold}.")


async def circle_fees(client: httpx.AsyncClient, base_url: str) -> list:
    url = f"{base_url.rstrip('/')}/v2/burn/USDC/fees/{SOURCE_DOMAIN}/{ARC_DOMAIN}"
    try:
        response = await client.get(url, timeout=15)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise UpstreamError("Circle's Base Sepolia to Arc fee quote is unavailable.") from exc


async def verify_source(rpc) -> None:
    if quantity(await rpc.call("eth_chainId")) != BASE_CHAIN_ID:
        raise ValueError("The RPC is not Base Sepolia (chain 84532). Nothing was sent.")
    for contract, name in ((MESSENGER, "TokenMessengerV2"), (USDC, "USDC")):
        if not hex_bytes(await rpc.call("eth_getCode", [contract, "latest"])):
            raise ValueError(f"No {name} contract code on this RPC. Nothing was sent.")


async def probe_route(rpc, plan: Plan, origin: str) -> str:
    """Simulate the burn: success, or a token-transfer revert, proves selector and domain."""
    ok, reason = await rpc.simulate({"from": origin, "to": MESSENGER, "data": plan.burn_data})
    if ok or any(text in reason.lower() for text in ROUTE_OK_REVERTS):
        return ("burn simulates successfully" if ok else
                "TokenMessengerV2 accepts the parameters; simulation stops at the USDC transfer")
    raise ValueError(f"TokenMessengerV2 rejected the burn parameters: {reason}. Nothing was sent.")


async def erc20_read(rpc, selector: bytes, *owners: str) -> int:
    data = "0x" + (selector + b"".join(word(owner) for owner in owners)).hex()
    return quantity(await rpc.call("eth_call", [{"to": USDC, "data": data}, "latest"]))


async def gas_prices(rpc) -> tuple[int, int]:
    block = await rpc.call("eth_getBlockByNumber", ["latest", False])
    if not isinstance(block, dict):
        raise UpstreamError("Base Sepolia RPC returned an unreadable block.")
    base_fee = quantity(block.get("baseFeePerGas"))
    tip = quantity(await rpc.call("eth_maxPriorityFeePerGas"))
    return 2 * base_fee + tip, tip


async def funding(rpc, wallet: str, plan: Plan, max_fee_per_gas: int) -> dict:
    balance = await erc20_read(rpc, BALANCE_OF, wallet)
    allowance = await erc20_read(rpc, ALLOWANCE, wallet, MESSENGER)
    eth = quantity(await rpc.call("eth_getBalance", [wallet, "latest"]))
    budget = BURN_GAS_BUDGET + (APPROVE_GAS_BUDGET if allowance < plan.amount else 0)
    return {"usdc": balance, "allowance": allowance, "eth": eth,
            "eth_needed": budget * max_fee_per_gas, "needs_approve": allowance < plan.amount}


def shortfall(wallet: str, plan: Plan, funds: dict) -> str | None:
    problems = []
    if funds["usdc"] < plan.amount:
        problems.append(
            f"{wallet} holds {usdc(funds['usdc'])} Base Sepolia USDC; the burn needs "
            f"{usdc(plan.amount)}. Request testnet USDC on Base Sepolia at {CIRCLE_FAUCET}."
        )
    if funds["eth"] < funds["eth_needed"]:
        problems.append(
            f"{wallet} holds {usdc(funds['eth'], decimals=18)} Base Sepolia ETH; execution needs up to "
            f"{usdc(funds['eth_needed'], decimals=18)}. Use a Base Sepolia faucet from {ETH_FAUCETS}."
        )
    return " ".join(problems) or None


def scan_window(burn_block: int, head: int) -> int:
    """Blocks from the head back to the burn, plus an hour of 2-second Base blocks."""
    return min(50_000, max(head - burn_block, 0) + 1 + 1_800)


def verify_burn_receipt(receipt: dict, tx_hash: str, sender: str, plan: Plan) -> Burn:
    if hash32(receipt.get("transactionHash", "")) != tx_hash:
        raise ValueError("Receipt is for a different transaction.")
    if str(receipt.get("from", "")).lower() != sender or str(receipt.get("to", "")).lower() != MESSENGER:
        raise ValueError("Receipt sender or contract does not match the signed burn.")
    logs = [log for log in receipt.get("logs") or [] if isinstance(log, dict)
            and str(log.get("address", "")).lower() == MESSENGER
            and str((log.get("topics") or [""])[0]).lower() == BURN_TOPIC]
    if len(logs) != 1:
        raise ValueError(f"Expected exactly one DepositForBurn event, found {len(logs)}.")
    burn = decode_burn(logs[0])
    if (burn.tx_hash != tx_hash or burn.block_number != quantity(receipt.get("blockNumber"))
            or burn.block_hash != hash32(receipt.get("blockHash"))):
        raise ValueError("DepositForBurn event identity does not match its receipt.")
    expected = {"amount": plan.amount, "mint_recipient": plan.recipient, "depositor": sender,
                "destination_domain": ARC_DOMAIN, "caller": ZERO, "hook": b"",
                "max_fee": plan.max_fee, "min_finality": plan.threshold}
    for name, value in expected.items():
        if getattr(burn, name) != value:
            raise ValueError(f"DepositForBurn {name} does not match the reviewed burn.")
    return burn


async def check_receipt(rpc, receipt: dict, tx_hash: str, sender: str, to: str) -> tuple[int, int]:
    if (not isinstance(receipt, dict) or hash32(receipt.get("transactionHash")) != tx_hash
            or address(receipt.get("from")) != sender or address(receipt.get("to")) != to):
        raise ValueError("Receipt identity does not match the signed transaction.")
    block_number = quantity(receipt.get("blockNumber"))
    block_hash = hash32(receipt.get("blockHash"))
    block = await rpc.call("eth_getBlockByNumber", [hex(block_number), False])
    if (not isinstance(block, dict) or quantity(block.get("number")) != block_number
            or hash32(block.get("hash")) != block_hash):
        raise ValueError("Receipt does not match the current Base Sepolia block.")
    status = quantity(receipt.get("status"))
    if status not in (0, 1):
        raise ValueError("Receipt status is invalid.")
    return status, block_number


async def send_once(rpc, account, sender: str, to: str, data: str, budget: int, label: str,
                    emit: Callable, sleep: Callable) -> dict:
    """Simulate, sign and broadcast exactly once. Unknown delivery never retries."""
    tx = {"from": sender, "to": to, "data": data}
    ok, reason = await rpc.simulate(tx)
    if not ok:
        raise ValueError(f"{label} simulation reverted: {reason}. This step was not sent.")
    if to == USDC and quantity(reason) != 1:
        raise ValueError("The approval simulation returned false. Nothing was sent.")
    gas = quantity(await rpc.call("eth_estimateGas", [tx]))
    gas_limit = (gas * 12 + 9) // 10
    if gas <= 0 or gas_limit > budget:
        raise ValueError(f"{label} gas estimate is outside the lab's bound. This step was not sent.")
    max_fee_per_gas, tip = await gas_prices(rpc)
    if gas_limit * max_fee_per_gas > EXECUTION_COST_CAP:
        raise ValueError(f"{label} execution gas exceeds 0.001 ETH. This step was not sent.")
    if quantity(await rpc.call("eth_getBalance", [sender, "pending"])) < gas_limit * max_fee_per_gas:
        raise ValueError(f"{label} needs more Base Sepolia ETH for execution gas. This step was not sent.")
    nonce = quantity(await rpc.call("eth_getTransactionCount", [sender, "pending"]))
    signed = account.sign_transaction({
        "type": 2, "chainId": BASE_CHAIN_ID, "nonce": nonce, "to": bytes.fromhex(to[2:]),
        "value": 0, "gas": gas_limit, "maxFeePerGas": max_fee_per_gas,
        "maxPriorityFeePerGas": tip, "data": data,
    })
    local_hash = "0x" + bytes(signed.hash).hex()
    emit(f"{label}: signed {local_hash}")
    try:
        remote = await rpc.call("eth_sendRawTransaction", ["0x" + bytes(signed.raw_transaction).hex()])
        if hash32(remote) != local_hash:
            raise ValueError("RPC returned a different transaction hash.")
    except Exception:
        emit(f"{label}: broadcast outcome unknown. Not resending. Inspect {BASESCAN}/tx/{local_hash}")
        return {"status": "broadcast_unknown", "step": label, "tx_hash": local_hash}
    try:
        async with asyncio.timeout(RECEIPT_TIMEOUT_SECONDS):
            for attempt in range(RECEIPT_POLLS):
                try:
                    receipt = await rpc.call("eth_getTransactionReceipt", [local_hash])
                except UpstreamError:
                    receipt = None
                if receipt is not None:
                    break
                if attempt + 1 < RECEIPT_POLLS:
                    await sleep(POLL_SECONDS)
    except TimeoutError:
        receipt = None
    if receipt is not None:
        try:
            status, block_number = await check_receipt(rpc, receipt, local_hash, sender, to)
        except (ValueError, UpstreamError):
            emit(f"{label}: receipt could not be verified. Inspect {BASESCAN}/tx/{local_hash}")
            return {"status": "receipt_unverified", "step": label, "tx_hash": local_hash}
        if status == 0:
            emit(f"{label}: reverted on chain. Gas was spent; no USDC moved in this step.")
            return {"status": "reverted", "step": label, "tx_hash": local_hash}
        emit(f"{label}: confirmed in block {block_number:,}")
        return {"status": "confirmed", "tx_hash": local_hash, "receipt": receipt}
    emit(f"{label}: still unconfirmed. Not resending. Check {BASESCAN}/tx/{local_hash} first.")
    return {"status": "submitted", "step": label, "tx_hash": local_hash}


async def interrupt(
    rpc,
    quote_fees: Callable[[], Awaitable[object]],
    *,
    amount: str,
    recipient: str,
    speed: str = "fast",
    sender: str | None = None,
    dry_run: bool = False,
    read_key: Callable = read_hidden_key,
    confirm: Callable = input,
    emit: Callable = print,
    sleep: Callable = asyncio.sleep,
) -> dict:
    units, recipient = parse_amount(amount), parse_recipient(recipient)
    sender = address(sender) if sender else None
    if speed not in FINALITY:
        raise ValueError("--speed must be fast or standard.")
    await verify_source(rpc)
    bps, max_fee = max_fee_from_quote(await quote_fees(), FINALITY[speed], units)
    plan = Plan(units, recipient, speed, FINALITY[speed], bps, max_fee)
    route = await probe_route(rpc, plan, sender or recipient)
    max_fee_per_gas, _ = await gas_prices(rpc)

    emit("REBOUND LAB - creates a controlled interrupted transfer. Not part of recovery.")
    emit(f"Source:            Base Sepolia (chain {BASE_CHAIN_ID}, CCTP domain {SOURCE_DOMAIN})")
    emit(f"From:              {sender or 'the address of the key you enter next'}")
    emit(f"Recipient on Arc:  {recipient}")
    emit(f"Amount burned:     {usdc(units)} USDC (canonical {USDC})")
    emit(f"maxFee:            {usdc(max_fee)} USDC (Circle quote {bps} bps + 20% buffer)")
    emit(f"Finality:          {speed} (minFinalityThreshold {plan.threshold}); "
         f"attestation {EXPECTED_WAIT[speed]}")
    emit(f"                   fast = {EXPECTED_WAIT['fast']}, standard = {EXPECTED_WAIT['standard']}")
    emit(f"Destination:       Arc Testnet, CCTP domain {ARC_DOMAIN}")
    emit("destinationCaller: 0x" + "00" * 32 + " (anyone may submit the mint)")
    emit("Hook:              none - plain depositForBurn, no Circle forwarding")
    emit(f"Route check:       {route}")
    emit(f"Execution gas:     at most 0.001 ETH per transaction (now {max_fee_per_gas:,} wei/gas)")
    emit("Base also charges a separate L1 data fee.")
    emit("This tool never calls receiveMessage. Do NOT mint this transfer any other way.")

    result = {"amount": usdc(units), "recipient": recipient, "max_fee": usdc(max_fee),
              "min_finality": plan.threshold, "fee_bps": str(bps)}
    if dry_run:
        emit(f"approve calldata (only if allowance < amount), to USDC: {plan.approve_data}")
        emit(f"depositForBurn calldata, to TokenMessengerV2 {MESSENGER}: {plan.burn_data}")
        if sender:
            funds = await funding(rpc, sender, plan, max_fee_per_gas)
            problem = shortfall(sender, plan, funds)
            emit(f"Sender holds {usdc(funds['usdc'])} USDC, {usdc(funds['eth'], decimals=18)} ETH; "
                 f"allowance {usdc(funds['allowance'])} USDC "
                 f"(approve {'needed' if funds['needs_approve'] else 'not needed'}).")
            emit(problem or "Sender covers the token amount and estimated execution gas.")
            result["funded"] = problem is None
        emit("Dry run: no key requested and nothing sent.")
        return {"status": "dry_run", **result, "approve_data": plan.approve_data,
                "burn_data": plan.burn_data}

    if confirm("Type INTERRUPT to burn this testnet USDC on Base Sepolia: ").strip() != "INTERRUPT":
        emit("Cancelled; no signing key requested and no transaction sent.")
        return {"status": "cancelled"}
    key = read_key("Throwaway Base Sepolia private key (hidden; never saved): ")
    try:
        account = Account.from_key(key.strip())
    except (ValueError, TypeError):
        raise ValueError("The local signing key is invalid.") from None
    finally:
        key = None  # Python cannot reliably erase strings; never promise secure erasure.
    wallet = account.address.lower()
    if sender and wallet != sender:
        raise ValueError("The signing key address does not match --sender. Nothing was sent.")
    emit(f"Signing address:   {wallet}")
    funds = await funding(rpc, wallet, plan, max_fee_per_gas)
    if problem := shortfall(wallet, plan, funds):
        raise ValueError(f"{problem} Nothing was sent.")
    result["sender"] = wallet

    if funds["needs_approve"]:
        step = await send_once(rpc, account, wallet, USDC, plan.approve_data, APPROVE_GAS_BUDGET,
                               "approve", emit, sleep)
        if step["status"] != "confirmed":
            return {**result, **step}
        result["approve_tx"] = step["tx_hash"]
        for attempt in range(5):  # Read-only: public RPC backends can lag one block.
            if await erc20_read(rpc, ALLOWANCE, wallet, MESSENGER) >= units:
                break
            if attempt == 4:
                raise ValueError("Allowance is still below the amount after approval. "
                                 "Burn not sent; rerun to skip the approval.")
            await sleep(POLL_SECONDS)
    step = await send_once(rpc, account, wallet, MESSENGER, plan.burn_data, BURN_GAS_BUDGET,
                           "depositForBurn", emit, sleep)
    if step["status"] != "confirmed":
        return {**result, **step}
    tx_hash = step["tx_hash"]
    try:
        burn = verify_burn_receipt(step["receipt"], tx_hash, wallet, plan)
    except (ValueError, UpstreamError) as exc:
        emit(f"Burn confirmed but NOT verified: {exc} Inspect {BASESCAN}/tx/{tx_hash}")
        return {**result, "status": "burn_unverified", "tx_hash": tx_hash}
    try:
        head = quantity(await rpc.call("eth_blockNumber"))
    except UpstreamError:
        head = burn.block_number
    blocks = scan_window(burn.block_number, head)
    end_block = burn.block_number + 100
    emit("")
    emit("Controlled interrupted transfer created. Source burn verified:")
    emit(f"  tx hash      {tx_hash}")
    emit(f"  log index    {burn.log_index}")
    emit(f"  block        {burn.block_number:,} ({burn.block_hash})")
    emit(f"  Basescan     {BASESCAN}/tx/{tx_hash}")
    emit(f"Circle attestation expected in {EXPECTED_WAIT[speed]}; Rebound reports Confirming "
         "for the first 12 source blocks.")
    emit("Next steps:")
    emit(f"  1. Open a FRESH Rebound session, scan {wallet} with --blocks {blocks}:")
    emit(f"     uv run python -m rebound.cli scan {wallet} --blocks {blocks}")
    emit("     (covers the burn for about an hour; add 1,800 blocks per extra hour, max 50,000)")
    if recipient != wallet:
        emit(f"     Scanning the recipient {recipient} finds the same burn.")
    emit(f"     Fixed window, once Base Sepolia passes block {end_block:,}: "
         f"--blocks 200 --end-block {end_block}")
    emit("  2. Finish it only with Rebound's recover command. Do NOT mint it any other way.")
    return {**result, "status": "interrupted", "tx_hash": tx_hash, "log_index": burn.log_index,
            "block_number": burn.block_number, "block_hash": burn.block_hash,
            "basescan": f"{BASESCAN}/tx/{tx_hash}", "scan_blocks": blocks}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="python -m rebound.lab",
        description="Demo lab only: create a controlled interrupted Base Sepolia -> Arc burn.",
    )
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "interrupt", help="Burn a small amount to Arc with no hook, then stop before the mint"
    )
    command.add_argument("--amount", required=True, help="USDC to burn (at most 5)")
    command.add_argument("--recipient", required=True, help="Arc Testnet address you control")
    command.add_argument("--speed", choices=sorted(FINALITY), default="fast",
                         help="fast (~8 s attestation, default) or standard (~15-19 min)")
    command.add_argument("--rpc", default=os.getenv("BASE_SEPOLIA_RPC", "https://sepolia.base.org"),
                         help="Base Sepolia RPC URL (default: BASE_SEPOLIA_RPC or public RPC)")
    command.add_argument("--sender", help="Expected signing address; checks funding in --dry-run")
    command.add_argument("--dry-run", action="store_true",
                         help="Run every read-only check and print calldata; never asks for a key")
    return result


async def run(args: argparse.Namespace) -> dict:
    circle_url = os.getenv("CIRCLE_SANDBOX_URL", "https://iris-api-sandbox.circle.com")
    arc_url = os.getenv("ARC_TESTNET_RPC", "https://rpc.testnet.arc.network")
    for endpoint in (args.rpc, arc_url, circle_url):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("RPC and Circle endpoints must be valid HTTP(S) URLs.")
    if not args.dry_run and not sys.stdin.isatty():
        raise ValueError("A live burn requires an interactive terminal; keys cannot be piped.")
    async with httpx.AsyncClient(timeout=25) as client:
        rpc = LabRpc(args.rpc, "Base Sepolia RPC", client)
        recipient = parse_recipient(args.recipient)
        await verify_recipient(Rpc(arc_url, "Arc Testnet RPC", client), recipient)
        return await interrupt(
            rpc, lambda: circle_fees(client, circle_url), amount=args.amount,
            recipient=recipient, speed=args.speed, sender=args.sender, dry_run=args.dry_run,
        )


def main() -> int:
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except (KeyboardInterrupt, EOFError):
        print("Stopped. If a signed hash was printed, inspect it before another attempt.",
              file=sys.stderr)
        return 130
    except Exception as exc:
        message = (str(exc) if isinstance(exc, (ValueError, UpstreamError))
                   else "Command failed; nothing was retried automatically.")
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") in {"interrupted", "dry_run", "cancelled"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
