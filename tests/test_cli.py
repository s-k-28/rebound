"""Signing tests use a fixed throwaway fixture key and never reach a network."""

import asyncio
from copy import deepcopy
from decimal import Decimal

import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from rebound import cli
from rebound.protocol import RECEIVED_TOPIC, TRANSMITTER, receive_calldata
from tests.test_protocol import MESSENGER, OTHER, message_hex, word_address

KEY = "0x" + "01" * 32
CALLER = Account.from_key(KEY).address.lower()
TX_HASH = "0x" + "cd" * 32
NONCE = "0x" + "ab" * 32
ARC_BLOCK_HASH = "0x" + "ef" * 32


class FakeArc:
    def __init__(self, *, gas_price=10**9, chain_id=5042002, uncertain=False, balance=10**18):
        self.gas_price = gas_price
        self.balance = balance
        self.chain_id = chain_id
        self.uncertain = uncertain
        self.broadcasts = []
        self.receipt_body = bytes.fromhex(message_hex()[2:])[148:]
        self.canonical_block_hash = ARC_BLOCK_HASH

    async def call(self, method, params=None):
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_gasPrice":
            return hex(self.gas_price)
        if method == "eth_getBalance":
            assert params == [CALLER, "pending"]
            return hex(self.balance)
        if method == "eth_getTransactionCount":
            assert params == [CALLER, "pending"]
            return "0x0"
        if method == "eth_sendRawTransaction":
            raw = bytes.fromhex(params[0][2:])
            assert Account.recover_transaction(raw).lower() == CALLER
            self.broadcasts.append(raw)
            if self.uncertain:
                raise RuntimeError("provider may have accepted the signed transaction")
            return "0x" + keccak(raw).hex()
        if method == "eth_getTransactionReceipt":
            return {
                "transactionHash": params[0],
                "status": "0x1",
                "to": TRANSMITTER,
                "from": CALLER,
                "blockNumber": "0x123",
                "blockHash": ARC_BLOCK_HASH,
                "transactionIndex": "0x0",
                "logs": [
                    {
                        "address": TRANSMITTER,
                        "topics": [
                            RECEIVED_TOPIC,
                            "0x" + CALLER[2:].rjust(64, "0"),
                            NONCE,
                            "0x" + (2000).to_bytes(32, "big").hex(),
                        ],
                        "data": "0x" + encode(["uint32", "bytes32", "bytes"],
                                              [6, word_address(MESSENGER), self.receipt_body]).hex(),
                    }
                ],
            }
        if method == "eth_getBlockByNumber":
            assert params == ["0x123", False]
            return {"number": "0x123", "hash": self.canonical_block_hash,
                    "transactions": ["0x" + keccak(self.broadcasts[-1]).hex()]}
        raise AssertionError(method)


class FakeService:
    def __init__(self, arc=None):
        self.arc = arc or FakeArc()
        self.preparations = 0
        self.second_error = None
        self.changed_recipient = False
        self.payload = {
            "transaction": {
                "from": CALLER,
                "to": TRANSMITTER,
                "data": receive_calldata(message_hex(), "0x" + "ab" * 65),
                "value": "0x0",
                "chainId": 5042002,
                "gas": "0x30d40",
            },
            "recipient": "0x" + "11" * 20,
            "amount": "128.5",
            "net_amount": "128.4995",
            "fee": "0.0005",
            "tx_hash": TX_HASH,
            "log_index": 3,
            "nonce": NONCE,
            "warning": "Simulation does not guarantee execution.",
        }

    async def prepare(self, tx_hash, log_index, caller):
        assert (tx_hash, log_index, caller) == (TX_HASH, 3, CALLER)
        self.preparations += 1
        if self.preparations == 2 and self.second_error:
            raise self.second_error
        result = deepcopy(self.payload)
        if self.preparations == 2 and self.changed_recipient:
            result["recipient"] = OTHER
            result["transaction"]["data"] = receive_calldata(message_hex(recipient=OTHER), "0x" + "ab" * 65)
        return result


async def no_sleep(_seconds):
    pass


async def run_recovery(service, answer="RECOVER", key=KEY):
    output = []
    result = await cli.recover_transfer(
        service,
        TX_HASH,
        3,
        CALLER,
        max_fee=Decimal("0.1"),
        read_key=lambda _prompt: key,
        confirm=lambda _prompt: answer,
        emit=output.append,
        sleep=no_sleep,
    )
    return result, output


async def test_wrong_signing_key_never_broadcasts():
    service = FakeService()
    with pytest.raises(ValueError, match="does not match"):
        await run_recovery(service, key="0x" + "02" * 32)
    assert service.arc.broadcasts == []


async def test_no_confirmation_never_broadcasts():
    service = FakeService()
    result, _ = await run_recovery(service, answer="yes")
    assert result["status"] == "cancelled"
    assert service.arc.broadcasts == []


async def test_wrong_network_never_broadcasts():
    service = FakeService(FakeArc(chain_id=1))
    with pytest.raises(ValueError, match="Arc Testnet"):
        await run_recovery(service)
    assert service.arc.broadcasts == []


async def test_gas_fee_above_cap_never_broadcasts():
    service = FakeService(FakeArc(gas_price=10**12))
    with pytest.raises(ValueError, match="fee cap"):
        await run_recovery(service)
    assert service.arc.broadcasts == []


async def test_unfunded_arc_caller_is_rejected_before_key_entry():
    service = FakeService(FakeArc(balance=0))

    def forbidden_key(_prompt):
        pytest.fail("A key was requested before checking the caller's balance")

    with pytest.raises(ValueError, match="needs 0.0002 more Arc Testnet USDC"):
        await cli.recover_transfer(service, TX_HASH, 3, CALLER, read_key=forbidden_key,
                                   confirm=lambda _: "RECOVER", emit=lambda _: None)
    assert service.arc.broadcasts == []


async def test_fresh_recheck_completed_nonce_stops_broadcast():
    service = FakeService()
    service.second_error = ValueError("Transfer is completed")
    with pytest.raises(ValueError, match="completed"):
        await run_recovery(service)
    assert service.arc.broadcasts == []


async def test_changed_preparation_after_confirmation_is_not_signed():
    service = FakeService()
    service.changed_recipient = True
    with pytest.raises(ValueError, match="changed"):
        await run_recovery(service)
    assert service.arc.broadcasts == []


async def test_valid_recovery_signs_once_and_reports_confirmed_receipt():
    service = FakeService()
    result, output = await run_recovery(service)
    assert result["status"] == "completed"
    assert result["receipt"]["status"] == "0x1"
    assert len(service.arc.broadcasts) == 1
    assert service.preparations == 2
    assert all(KEY not in line for line in output)


async def test_recovery_only_reports_completion_for_a_canonical_receipt():
    service = FakeService()
    service.arc.canonical_block_hash = "0x" + "a1" * 32
    result, output = await run_recovery(service)
    assert result["status"] == "receipt_unverified"
    assert result["tx_hash"] == "0x" + keccak(service.arc.broadcasts[0]).hex()
    assert len(service.arc.broadcasts) == 1
    assert any("Could not verify the canonical Arc mint receipt" in line for line in output)


async def test_receipt_deadline_preserves_signed_hash_without_resending(monkeypatch):
    class SlowReceiptArc(FakeArc):
        async def call(self, method, params=None):
            if method == "eth_getTransactionReceipt":
                await asyncio.Future()
            return await super().call(method, params)

    monkeypatch.setattr(cli, "RECEIPT_TIMEOUT_SECONDS", 0.01)
    service = FakeService(SlowReceiptArc())
    result, output = await run_recovery(service)
    assert result["status"] == "submitted"
    assert result["tx_hash"] == "0x" + keccak(service.arc.broadcasts[0]).hex()
    assert len(service.arc.broadcasts) == 1
    assert any("Check the destination hash" in line for line in output)


async def test_uncertain_broadcast_reports_hash_without_resubmitting():
    service = FakeService(FakeArc(uncertain=True))
    result, _ = await run_recovery(service)
    assert result["status"] == "broadcast_unknown"
    assert result["tx_hash"].startswith("0x")
    assert len(result["tx_hash"]) == 66
    assert len(service.arc.broadcasts) == 1


@pytest.mark.parametrize("change", [{"chainId": 1}, {"value": "0x1"}, {"to": "0x" + "44" * 20}])
async def test_tampered_unsigned_transaction_never_broadcasts(change):
    service = FakeService()
    service.payload["transaction"].update(change)
    with pytest.raises(ValueError):
        await run_recovery(service)
    assert service.arc.broadcasts == []


@pytest.mark.parametrize("change", [{"recipient": OTHER}, {"net_amount": "128.5"}, {"log_index": 4}])
async def test_mismatched_review_never_requests_a_key(change):
    service = FakeService()
    service.payload.update(change)

    def forbidden_key(_prompt):
        pytest.fail("A key was requested for a mismatched review")

    with pytest.raises(ValueError, match="review details|source burn"):
        await cli.recover_transfer(service, TX_HASH, 3, CALLER, read_key=forbidden_key,
                                   confirm=lambda _: "RECOVER", emit=lambda _: None)
    assert service.arc.broadcasts == []


async def test_matching_nonce_without_matching_message_body_is_not_verified():
    service = FakeService()
    service.arc.receipt_body = bytes.fromhex(message_hex(recipient=OTHER)[2:])[148:]
    result, _ = await run_recovery(service)
    assert result["status"] == "receipt_unverified"
    assert len(service.arc.broadcasts) == 1
