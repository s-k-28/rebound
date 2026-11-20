"""Canonical CCTP V2 layouts, independently validated against original burn events.

Layouts: Circle MessageV2.sol and BurnMessageV2.sol (Apache-2.0).
This module implements parsing, not signature verification: destination simulation
and the canonical MessageTransmitter verify Circle's signatures.
"""
import re
from dataclasses import dataclass

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

ZERO = "0x" + "00" * 20
MESSENGER = "0x8fe6b999dc680ccfdd5bf7eb0974218be2542daa"
TRANSMITTER = "0xe737e5cebeeba77efe34d4aa090756590b1ce275"
USDC = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
BURN_TOPIC = "0x" + keccak(text="DepositForBurn(address,uint256,address,bytes32,uint32,bytes32,bytes32,uint256,uint32,bytes)").hex()
RECEIVED_TOPIC = "0x" + keccak(text="MessageReceived(address,uint32,bytes32,bytes32,uint32,bytes)").hex()
RECEIVE_SELECTOR = keccak(text="receiveMessage(bytes,bytes)")[:4]
FORWARDING_MAGIC = b"cctp-forward".ljust(24, b"\x00")
ARC_EXPLORER = "https://explorer.testnet.arc.io"


def address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("Enter a complete EVM wallet address (0x followed by 40 hex characters).")
    return value.lower()


def hash32(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("Expected a full 32-byte transaction hash.")
    return value.lower()


def hex_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2:
        raise ValueError("Invalid hex-encoded data.")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise ValueError("Invalid hex-encoded data.") from exc


def evm_word(value: bytes) -> str:
    if len(value) != 32 or any(value[:12]):
        raise ValueError("Invalid EVM address padding in message.")
    return "0x" + value[12:].hex()


def usdc(amount: int, *, decimals: int = 6) -> str:
    whole, fraction = divmod(amount, 10**decimals)
    return f"{whole}.{fraction:0{decimals}d}".rstrip("0") if fraction else str(whole)


@dataclass(frozen=True)
class Burn:
    tx_hash: str
    log_index: int
    block_hash: str
    block_number: int
    amount: int
    depositor: str
    mint_recipient: str
    destination_domain: int
    caller: str
    max_fee: int
    min_finality: int
    hook: bytes = b""

    @property
    def key(self) -> str:
        return f"{self.tx_hash}:{self.log_index}"


@dataclass(frozen=True)
class Message:
    source_domain: int
    destination_domain: int
    nonce: str
    sender: str
    recipient: str
    caller: str
    min_finality: int
    executed_finality: int
    burn_token: str
    mint_recipient: str
    amount: int
    depositor: str
    max_fee: int
    fee: int
    expires_at: int
    hook: bytes


def decode_message(value: str) -> Message:
    raw = hex_bytes(value)
    if len(raw) < 376:
        raise ValueError("CCTP V2 message is too short.")
    def number(start, length=32):
        return int.from_bytes(raw[start:start + length], "big")
    if number(0, 4) != 1 or number(148, 4) != 1:
        raise ValueError("Unsupported CCTP message version.")
    return Message(
        source_domain=number(4, 4), destination_domain=number(8, 4),
        nonce="0x" + raw[12:44].hex(), sender=evm_word(raw[44:76]),
        recipient=evm_word(raw[76:108]), caller=evm_word(raw[108:140]),
        min_finality=number(140, 4), executed_finality=number(144, 4),
        burn_token=evm_word(raw[152:184]), mint_recipient=evm_word(raw[184:216]),
        amount=number(216), depositor=evm_word(raw[248:280]), max_fee=number(280),
        fee=number(312), expires_at=number(344), hook=raw[376:],
    )


def matches_mint_receipt(receipt: object, tx_hash: str, raw_message: str, message: Message) -> bool:
    """Match Circle's suggested destination hash to an exact onchain receipt."""
    if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
        return False
    try:
        if hash32(receipt.get("transactionHash")) != tx_hash:
            return False
    except ValueError:
        return False
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return False
    expected_body = hex_bytes(raw_message)[148:]
    for log in logs:
        if not isinstance(log, dict) or log.get("removed") or str(log.get("address", "")).lower() != TRANSMITTER:
            continue
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) != 4:
            continue
        try:
            if hash32(topics[0]) != RECEIVED_TOPIC or hash32(topics[2]) != message.nonce:
                continue
            caller = evm_word(hex_bytes(hash32(topics[1])))
            if message.caller != ZERO and caller != message.caller:
                continue
            source_domain, sender, body = decode(["uint32", "bytes32", "bytes"], hex_bytes(log.get("data")))
            if (source_domain == message.source_domain and evm_word(sender) == message.sender
                    and body == expected_body and int(hash32(topics[3]), 16) == message.executed_finality):
                return True
        except (DecodingError, ValueError):
            continue
    return False


def decode_burn(log: dict) -> Burn:
    topics = log.get("topics", [])
    if log.get("removed") or log.get("address", "").lower() != MESSENGER:
        raise ValueError("Burn log is removed or from an unsupported contract.")
    if len(topics) != 4 or topics[0].lower() != BURN_TOPIC:
        raise ValueError("Not a supported CCTP V2 burn log.")
    if evm_word(hex_bytes(topics[1])) != USDC:
        raise ValueError("Burn is not canonical Base Sepolia USDC.")
    try:
        amount, recipient, domain, remote, caller, max_fee, hook = decode(
            ["uint256", "bytes32", "uint32", "bytes32", "bytes32", "uint256", "bytes"],
            hex_bytes(log["data"]),
        )
        if evm_word(remote) != MESSENGER or amount <= 0 or max_fee >= amount:
            raise ValueError("Unsupported destination messenger or invalid amount/fee.")
        return Burn(
            tx_hash=hash32(log["transactionHash"]), log_index=int(log["logIndex"], 16),
            block_hash=hash32(log["blockHash"]), block_number=int(log["blockNumber"], 16),
            amount=amount, depositor=evm_word(hex_bytes(topics[2])),
            mint_recipient=evm_word(recipient), destination_domain=domain,
            caller=evm_word(caller), max_fee=max_fee, min_finality=int(topics[3], 16), hook=hook,
        )
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Malformed burn event.") from exc
    except Exception as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("Malformed burn event.") from exc


def forwarding_hook_kind(hook: bytes) -> str | None:
    """Recognize documented EVM forwarding frames for inspection only."""
    if len(hook) < 32 or hook[:24] != FORWARDING_MAGIC or any(hook[28:32]):
        return None
    version = int.from_bytes(hook[24:28], "big")
    if version == 0:
        return "circle_forwarding" if len(hook) == 32 else "forwarding_with_app_hook"
    if version != 1:
        return None
    offset = 32
    while offset < len(hook):
        frame = hook[offset:offset + 32]
        if len(frame) != 32 or frame[:24] == FORWARDING_MAGIC or int.from_bytes(frame[24:28], "big") != 1:
            return None
        offset += 32 + int.from_bytes(frame[28:32], "big")
    if offset != len(hook):
        return None
    return "circle_forwarding" if len(hook) == 32 else "forwarding_with_app_hook"


def validate_message(message: Message, *, inspect_forwarding: bool = False) -> None:
    if (message.source_domain, message.destination_domain) != (6, 26):
        raise ValueError("Unsupported source or destination domain.")
    if message.sender != MESSENGER or message.recipient != MESSENGER or message.burn_token != USDC:
        raise ValueError("Message token or messenger does not match the supported route.")
    if message.fee > message.max_fee or message.fee >= message.amount:
        raise ValueError("Attested fee exceeds the original allowance.")
    if message.executed_finality < message.min_finality:
        raise ValueError("Attested finality is below the original threshold.")
    if message.hook and not (inspect_forwarding and forwarding_hook_kind(message.hook)):
        raise ValueError("Transfers with hooks require an adapter and are not supported yet.")
    if message.mint_recipient == ZERO or message.nonce == "0x" + "00" * 32:
        raise ValueError("Invalid zero recipient or nonce.")


def validate_match(burn: Burn, message: Message, *, inspect_forwarding: bool = False) -> None:
    validate_message(message, inspect_forwarding=inspect_forwarding)
    for name in ["amount", "mint_recipient", "depositor", "destination_domain", "caller", "max_fee", "min_finality", "hook"]:
        if getattr(burn, name) != getattr(message, name):
            raise ValueError(f"Attestation does not match the original burn: {name}.")


def classify(message: Message, *, nonce_used: bool, destination_block: int, caller: str | None = None) -> str:
    if nonce_used:
        return "completed"
    # TokenMessengerV2 requires expirationBlock > block.number, and a new delivery
    # executes no earlier than the block after the observed destination head.
    if message.expires_at and destination_block + 1 >= message.expires_at:
        return "expired"
    if message.caller != ZERO and (caller is None or address(caller) != message.caller):
        return "caller_required"
    return "ready"


def receive_calldata(message: str, attestation: str) -> str:
    raw, signatures = hex_bytes(message), hex_bytes(attestation)
    if len(signatures) == 0 or len(signatures) % 65:
        raise ValueError("Attestation must contain complete 65-byte signatures.")
    return "0x" + (RECEIVE_SELECTOR + encode(["bytes", "bytes"], [raw, signatures])).hex()


def decode_receive_calldata(value: str) -> tuple[str, Message]:
    payload = hex_bytes(value)
    if payload[:4] != RECEIVE_SELECTOR:
        raise ValueError("Expected receiveMessage calldata.")
    try:
        raw, signatures = decode(["bytes", "bytes"], payload[4:])
    except DecodingError as exc:
        raise ValueError("Malformed receiveMessage calldata.") from exc
    raw_message = "0x" + raw.hex()
    if receive_calldata(raw_message, "0x" + signatures.hex()) != value.lower():
        raise ValueError("Expected canonical receiveMessage calldata.")
    message = decode_message(raw_message)
    validate_message(message)
    return raw_message, message


def used_nonce_calldata(nonce: str) -> str:
    return "0x" + (keccak(text="usedNonces(bytes32)")[:4] + hex_bytes(hash32(nonce))).hex()
