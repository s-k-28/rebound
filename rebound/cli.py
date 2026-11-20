"""Local-only, testnet-only recovery commands.

No web endpoint receives a signing key. A recovery signs only after a fresh
service preparation and explicit terminal confirmation; it never burns USDC.
"""

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from eth_account import Account

from rebound.protocol import (
    TRANSMITTER,
    ZERO,
    Message,
    address,
    decode_receive_calldata,
    hash32,
    matches_mint_receipt,
    usdc,
)

ARC_CHAIN_ID = 5042002


def integer(value: str | int) -> int:
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


def validate_prepared(prepared: dict, caller: str, source_hash: str, log_index: int) -> tuple[dict, str, Message]:
    """An additional local boundary around the service's unsigned payload."""
    tx = prepared["transaction"]
    if integer(tx["chainId"]) != ARC_CHAIN_ID:
        raise ValueError("Recovery is supported only on Arc Testnet (chain 5042002).")
    if address(tx["to"]) != TRANSMITTER or address(tx["from"]) != caller:
        raise ValueError("Prepared transaction has an unexpected contract or caller.")
    if integer(tx["value"]) != 0:
        raise ValueError("Prepared transaction is not a zero-value receiveMessage call.")
    if integer(tx["gas"]) <= 0:
        raise ValueError("Prepared transaction has an invalid gas limit.")
    if hash32(prepared["tx_hash"]) != source_hash or prepared["log_index"] != log_index:
        raise ValueError("Prepared transaction identifies a different source burn.")
    raw, message = decode_receive_calldata(tx["data"])
    if message.caller not in (ZERO, caller):
        raise ValueError("Prepared message restricts the destination caller.")
    if (address(prepared["recipient"]) != message.mint_recipient
            or hash32(prepared["nonce"]) != message.nonce
            or prepared["amount"] != usdc(message.amount)
            or prepared["net_amount"] != usdc(message.amount - message.fee)
            or prepared["fee"] != usdc(message.fee)):
        raise ValueError("Prepared review details do not match the encoded message.")
    return tx, raw, message


def fee_in_usdc(gas: int, price: int, cap: Decimal) -> Decimal:
    fee = Decimal(gas * price) / Decimal(10**18)
    if not cap.is_finite() or cap <= 0 or price <= 0 or fee > cap:
        raise ValueError("Estimated gas exceeds the local fee cap or the fee cap is invalid.")
    return fee


async def recover_transfer(
    service: Any,
    tx_hash: str,
    log_index: int,
    caller: str,
    *,
    max_fee: Decimal = Decimal("0.1"),
    read_key: Callable = getpass.getpass,
    confirm: Callable = input,
    emit: Callable = print,
    sleep: Callable = asyncio.sleep,
) -> dict:
    """Sign locally and submit at most once. Unknown delivery never retries."""
    tx_hash, caller = hash32(tx_hash), address(caller)
    if log_index < 0:
        raise ValueError("Log index must be nonnegative.")
    prepared = await service.prepare(tx_hash, log_index, caller)
    tx, _, _ = validate_prepared(prepared, caller, tx_hash, log_index)
    if integer(await service.arc.call("eth_chainId")) != ARC_CHAIN_ID:
        raise ValueError("Configured destination RPC is not Arc Testnet.")
    price = integer(await service.arc.call("eth_gasPrice"))
    approved_fee = fee_in_usdc(integer(tx["gas"]), price, max_fee)

    emit("Testnet recovery: Base Sepolia → Arc Testnet (chain 5042002)")
    emit(f"Source burn: {tx_hash}, log {log_index}")
    emit(f"Fixed recipient: {prepared['recipient']}")
    emit(f"Original amount: {prepared['amount']} USDC")
    emit(f"Recipient receives: {prepared['net_amount']} USDC; protocol fee: {prepared['fee']} USDC")
    emit(f"Estimated maximum gas cost: {approved_fee:f} testnet USDC")
    emit(f"Local fee cap: {max_fee:f} testnet USDC")
    emit(
        "No new burn is created. Racing attempts may still cost gas. Simulation does not guarantee execution."
    )
    if confirm("Type RECOVER to approve this testnet transaction: ").strip() != "RECOVER":
        emit("Cancelled; no signing key requested and no transaction sent.")
        return {"status": "cancelled"}

    try:
        key = read_key("Throwaway Arc Testnet private key (hidden; never saved): ").strip()
        account = Account.from_key(key)
    except (ValueError, TypeError):
        raise ValueError("The local signing key is invalid.") from None
    finally:
        # Python strings cannot be reliably zeroized; never promise secure memory erasure.
        key = None
    if account.address.lower() != caller:
        raise ValueError("The signing key address does not match the approved caller.")

    # No prior inbox state or exported transaction is trusted for broadcast.
    fresh = await service.prepare(tx_hash, log_index, caller)
    latest_tx, raw_message, message = validate_prepared(fresh, caller, tx_hash, log_index)
    if latest_tx["data"].lower() != tx["data"].lower():
        raise ValueError("Transfer details changed after review. Inspect and approve it again.")
    if integer(await service.arc.call("eth_chainId")) != ARC_CHAIN_ID:
        raise ValueError("Destination RPC changed and is not Arc Testnet.")
    latest_price = integer(await service.arc.call("eth_gasPrice"))
    latest_fee = fee_in_usdc(integer(latest_tx["gas"]), latest_price, max_fee)
    if latest_fee > approved_fee:
        raise ValueError("Estimated gas cost changed after review. Inspect and approve it again.")
    account_nonce = integer(await service.arc.call("eth_getTransactionCount", [caller, "pending"]))
    signed = account.sign_transaction(
        {
            "chainId": ARC_CHAIN_ID,
            "nonce": account_nonce,
            "to": bytes.fromhex(TRANSMITTER[2:]),
            "value": 0,
            "gas": integer(latest_tx["gas"]),
            "gasPrice": latest_price,
            "data": latest_tx["data"],
        }
    )
    local_hash = "0x" + bytes(signed.hash).hex()
    raw = "0x" + bytes(signed.raw_transaction).hex()
    emit(f"Signed transaction hash: {local_hash}")
    try:
        remote_hash = await service.arc.call("eth_sendRawTransaction", [raw])
        if hash32(remote_hash) != local_hash:
            raise ValueError("RPC returned a different transaction hash.")
    except Exception:
        emit(
            "Broadcast outcome is unknown. Do not submit again automatically; inspect the signed hash first."
        )
        return {"status": "broadcast_unknown", "tx_hash": local_hash}

    emit("Submitted once. Waiting up to 60 seconds for a destination receipt.")
    for attempt in range(13):
        try:
            receipt = await service.arc.call("eth_getTransactionReceipt", [local_hash])
        except Exception:
            emit(
                "Receipt lookup is unavailable. Check the destination hash before another attempt."
            )
            return {"status": "submitted", "tx_hash": local_hash}
        if receipt:
            if hash32(receipt.get("transactionHash", "")) != local_hash:
                raise ValueError("Destination receipt does not match the signed transaction.")
            if integer(receipt.get("status", 0)) != 1:
                emit(
                    "Destination transaction reverted; gas may have been spent. Re-diagnose the original burn."
                )
                return {"status": "reverted", "tx_hash": local_hash, "receipt": receipt}
            if (
                str(receipt.get("to", "")).lower() != TRANSMITTER
                or str(receipt.get("from", "")).lower() != caller
                or not matches_mint_receipt(receipt, local_hash, raw_message, message)
            ):
                emit(
                    "Receipt confirmed but the expected mint event was not verified. Inspect the transaction."
                )
                return {"status": "receipt_unverified", "tx_hash": local_hash, "receipt": receipt}
            emit(f"Destination mint confirmed: https://explorer.testnet.arc.io/tx/{local_hash}")
            return {"status": "completed", "tx_hash": local_hash, "receipt": receipt}
        if attempt < 12:
            await sleep(5)
    emit("Still awaiting a receipt. Keep this hash and check it before any retry.")
    return {"status": "submitted", "tx_hash": local_hash}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="python -m rebound.cli",
        description="Find and diagnose supported USDC transfers; local recovery is Arc Testnet only.",
    )
    commands = result.add_subparsers(dest="command", required=True)
    scan = commands.add_parser(
        "scan", help="Find transfers by public address in bounded source history"
    )
    scan.add_argument("wallet")
    scan.add_argument("--blocks", type=int, default=2000)
    scan.add_argument("--end-block", type=int, help="Use a fixed Base Sepolia block as the end of the scan")
    index = commands.add_parser("stream-index", help="Find and verify transfers from received Stream events")
    index.add_argument("wallet")
    lookup = commands.add_parser("inspect", help="Find supported burns within one Base Sepolia receipt")
    lookup.add_argument("tx_hash", help="Original Base Sepolia transaction hash")
    for name in ["prepare", "recover"]:
        command = commands.add_parser(
            name,
            help=(
                "Export a verified unsigned transaction"
                if name == "prepare"
                else "Review, sign locally and send one testnet mint"
            ),
        )
        command.add_argument("tx_hash", help="Original Base Sepolia burn transaction hash")
        command.add_argument(
            "log_index", type=int, help="DepositForBurn log index, as a decimal integer"
        )
        command.add_argument(
            "--caller", required=True, help="Arc Testnet address that would submit receiveMessage"
        )
        if name == "recover":
            command.add_argument(
                "--max-gas-usdc", default="0.1", help="Maximum testnet USDC gas cost (default: 0.1)"
            )
    return result


async def run(args: argparse.Namespace) -> dict:
    from rebound.service import RecoveryService, Settings

    async with httpx.AsyncClient(timeout=25) as client:
        service = RecoveryService(Settings.from_env(), client)
        if args.command == "scan":
            return await service.scan(args.wallet, blocks=args.blocks, end_block=args.end_block)
        if args.command == "stream-index":
            return await service.scan_stream(args.wallet)
        if args.command == "inspect":
            return await service.inspect_transaction(args.tx_hash)
        if args.command == "prepare":
            return await service.prepare(args.tx_hash, args.log_index, args.caller)
        if not sys.stdin.isatty():
            raise ValueError(
                "Recovery requires an interactive terminal; keys cannot be piped into this command."
            )
        try:
            cap = Decimal(args.max_gas_usdc)
        except InvalidOperation:
            raise ValueError("--max-gas-usdc must be a positive decimal amount.") from None
        return await recover_transfer(
            service, args.tx_hash, args.log_index, args.caller, max_fee=cap
        )


def main() -> int:
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except (KeyboardInterrupt, EOFError):
        print(
            "Stopped. If a signed transaction hash was printed, inspect it before another attempt.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        # Client/service errors are deliberately sanitized at their own boundary.
        from rebound.clients import UpstreamError

        message = (
            str(exc)
            if isinstance(exc, (ValueError, UpstreamError))
            else "Command failed; no automatic retry was attempted."
        )
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return (
        0
        if result.get("status") not in {"reverted", "broadcast_unknown", "receipt_unverified"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
