"""Lab tests use a fixed throwaway fixture key and RPC doubles; they never reach a network."""

import asyncio
import getpass
import warnings
from decimal import Decimal

import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes

from rebound import lab
from rebound.clients import UpstreamError
from rebound.protocol import BURN_TOPIC, MESSENGER, TRANSMITTER, USDC

KEY = "0x" + "01" * 32
SENDER = Account.from_key(KEY).address.lower()
RECIPIENT = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
FEES = [{"finalityThreshold": 1000, "minimumFee": 1.3}, {"finalityThreshold": 2000, "minimumFee": 0}]
BURN_BLOCK = 61_200_000
BURN_BLOCK_HASH = "0x" + "ef" * 32
ALLOWANCE_REVERT = "execution reverted: ERC20: transfer amount exceeds allowance"


def pad(value: str) -> bytes:
    return bytes.fromhex(value[2:]).rjust(32, b"\0")


def uint(value: int) -> bytes:
    return value.to_bytes(32, "big")


class FakeBase:
    def __init__(self, *, chain_id=84532, usdc=5_000_000, eth=10**17, allowance=0, code="0x6080",
                 uncertain=False, receipts=True, approve_status="0x1", route=ALLOWANCE_REVERT,
                 log_changes=None, approval_lands=True, canonical=True):
        self.chain_id, self.usdc, self.eth, self.allowance, self.code = chain_id, usdc, eth, allowance, code
        self.uncertain, self.receipts, self.approve_status = uncertain, receipts, approve_status
        self.route, self.log_changes, self.approval_lands = route, log_changes or {}, approval_lands
        self.canonical = canonical
        self.broadcasts, self.sent, self.simulations = [], {}, []

    async def call(self, method, params=None):
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getCode":
            assert params[0] in (MESSENGER, USDC)
            return self.code
        if method == "eth_getBlockByNumber":
            return {"number": hex(BURN_BLOCK), "baseFeePerGas": hex(5_000_000),
                    "hash": BURN_BLOCK_HASH if self.canonical else "0x" + "ab" * 32}
        if method == "eth_maxPriorityFeePerGas":
            return hex(1_000_000)
        if method == "eth_blockNumber":
            return hex(BURN_BLOCK + 20)
        if method == "eth_getBalance":
            assert params in ([SENDER, "latest"], [SENDER, "pending"])
            return hex(self.eth)
        if method == "eth_call":
            call, data = params[0], params[0]["data"]
            assert call["to"] == USDC
            if data.startswith("0x70a08231"):
                assert data == "0x70a08231" + pad(SENDER).hex()
                return "0x" + uint(self.usdc).hex()
            assert data == "0xdd62ed3e" + pad(SENDER).hex() + pad(MESSENGER).hex()
            return "0x" + uint(self.allowance).hex()
        if method == "eth_estimateGas":
            return hex(50_000 if params[0]["to"] == USDC else 150_000)
        if method == "eth_getTransactionCount":
            assert params == [SENDER, "pending"]
            return hex(len(self.broadcasts))
        if method == "eth_sendRawTransaction":
            raw = bytes.fromhex(params[0][2:])
            assert Account.recover_transaction(raw).lower() == SENDER
            tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
            tx["to"], tx["data"] = "0x" + bytes(tx["to"]).hex(), "0x" + bytes(tx["data"]).hex()
            self.broadcasts.append(tx)
            if self.uncertain:
                raise RuntimeError("provider may have accepted the signed transaction")
            tx_hash = "0x" + keccak(raw).hex()
            self.sent[tx_hash] = tx
            if tx["to"] == USDC and self.approval_lands:
                self.allowance = decode(["address", "uint256"], bytes.fromhex(tx["data"][10:]))[1]
            return tx_hash
        if method == "eth_getTransactionReceipt":
            if not self.receipts:
                return None
            tx = self.sent[params[0]]
            receipt = {"transactionHash": params[0], "from": SENDER, "to": tx["to"],
                       "blockNumber": hex(BURN_BLOCK), "blockHash": BURN_BLOCK_HASH,
                       "status": "0x1", "logs": []}
            if tx["to"] == USDC:
                receipt["status"] = self.approve_status
            else:
                receipt["logs"] = [self.burn_log(params[0], tx["data"])]
            return receipt
        raise AssertionError(method)

    async def simulate(self, tx):
        self.simulations.append(tx)
        if tx["to"] == MESSENGER and self.allowance < 1_000_000:
            return False, self.route
        return True, "0x" + uint(1).hex() if tx["to"] == USDC else "0x"

    def burn_log(self, tx_hash, data):
        fields = decode(["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
                        bytes.fromhex(data[10:]))
        amount, domain, recipient, _token, caller, max_fee, threshold = fields
        values = {"amount": amount, "recipient": recipient, "domain": domain, "caller": caller,
                  "max_fee": max_fee, "hook": b"", **self.log_changes}
        return {
            "address": MESSENGER,
            "topics": [BURN_TOPIC, "0x" + pad(USDC).hex(), "0x" + pad(SENDER).hex(),
                       "0x" + uint(threshold).hex()],
            "data": "0x" + encode(
                ["uint256", "bytes32", "uint32", "bytes32", "bytes32", "uint256", "bytes"],
                [values["amount"], values["recipient"], values["domain"], pad(MESSENGER),
                 values["caller"], values["max_fee"], values["hook"]]).hex(),
            "transactionHash": tx_hash, "blockHash": BURN_BLOCK_HASH,
            "blockNumber": hex(BURN_BLOCK), "logIndex": "0x7", "removed": False,
            **{key: value for key, value in self.log_changes.items()
               if key in {"blockNumber", "blockHash", "transactionHash"}},
        }


async def fees():
    return FEES


async def no_sleep(_seconds):
    pass


def forbidden(_prompt):
    pytest.fail("A prompt appeared before the guarded check")


async def run_lab(base, *, answer="INTERRUPT", key=KEY, quote=fees, **options):
    output = []
    result = await lab.interrupt(
        base, quote, amount=options.pop("amount", "1"), recipient=RECIPIENT,
        read_key=options.pop("read_key", lambda _prompt: key),
        confirm=options.pop("confirm", lambda _prompt: answer),
        emit=output.append, sleep=options.pop("sleep", no_sleep), **options,
    )
    return result, output


def test_calldata_matches_independent_encoding():
    approve = "0x095ea7b3" + (pad(MESSENGER) + uint(1_000_000)).hex()
    burn = "0x8e0250ee" + (uint(1_000_000) + uint(26) + pad(RECIPIENT) + pad(USDC) + bytes(32)
                           + uint(156) + uint(1000)).hex()
    assert lab.approve_calldata(1_000_000) == approve
    assert lab.burn_calldata(1_000_000, RECIPIENT, 156, 1000) == burn
    assert len(bytes.fromhex(burn[2:])) == 4 + 7 * 32  # no dynamic hook argument


def test_selector_is_the_plain_circle_v2_burn_not_the_hook_variant():
    assert lab.DEPOSIT_SELECTOR.hex() == "8e0250ee"
    hook = keccak(text="depositForBurnWithHook(uint256,uint32,bytes32,address,bytes32,uint256,uint32,bytes)")
    assert lab.DEPOSIT_SELECTOR != hook[:4]
    assert lab.APPROVE_SELECTOR.hex() == "095ea7b3"


def test_fee_quote_is_basis_points_with_buffer_rounded_up():
    assert lab.max_fee_from_quote(FEES, 1000, 1_000_000) == (Decimal("1.3"), 156)
    assert lab.max_fee_from_quote(FEES, 1000, 5_000_000) == (Decimal("1.3"), 780)
    assert lab.max_fee_from_quote(FEES, 2000, 1_000_000) == (Decimal("0"), 0)


@pytest.mark.parametrize("amount", ["5.000001", "1e100", "0", "-1", "0.0000001", "abc", "NaN"])
def test_amount_outside_the_lab_bound_is_refused(amount):
    with pytest.raises(ValueError):
        lab.parse_amount(amount)


@pytest.mark.parametrize("recipient", ["0x" + "00" * 20, MESSENGER, TRANSMITTER, USDC, "0x1234"])
def test_recipient_must_be_a_wallet(recipient):
    with pytest.raises(ValueError):
        lab.parse_recipient(recipient)


def test_fee_quote_above_bound_or_missing_is_refused():
    with pytest.raises(ValueError, match="bound"):
        lab.max_fee_from_quote([{"finalityThreshold": 1000, "minimumFee": 100}], 1000, 5_000_000)
    with pytest.raises(UpstreamError, match="threshold 1000"):
        lab.max_fee_from_quote([{"finalityThreshold": 2000, "minimumFee": 0}], 1000, 1_000_000)
    with pytest.raises(UpstreamError):
        lab.max_fee_from_quote([{"finalityThreshold": 1000, "minimumFee": "1.3"}], 1000, 1_000_000)


async def test_wrong_chain_is_refused_before_quote_and_prompts():
    async def no_quote():
        pytest.fail("Fee quote requested before the chain check")

    base = FakeBase(chain_id=5042002)
    with pytest.raises(ValueError, match="not Base Sepolia"):
        await run_lab(base, quote=no_quote, confirm=forbidden, read_key=forbidden)
    assert base.broadcasts == []


async def test_missing_contract_code_is_refused():
    base = FakeBase(code="0x")
    with pytest.raises(ValueError, match="contract code"):
        await run_lab(base, confirm=forbidden, read_key=forbidden)


async def test_unexpected_route_revert_is_refused_before_confirmation():
    base = FakeBase(route="execution reverted")
    with pytest.raises(ValueError, match="rejected the burn parameters"):
        await run_lab(base, confirm=forbidden, read_key=forbidden)
    assert base.broadcasts == []


async def test_dry_run_checks_everything_without_a_key_or_funds():
    base = FakeBase(usdc=0, eth=0)
    result, output = await run_lab(base, dry_run=True, sender=SENDER,
                                   confirm=forbidden, read_key=forbidden)
    assert result["status"] == "dry_run"
    assert result["funded"] is False
    assert result["burn_data"] == lab.burn_calldata(1_000_000, RECIPIENT, 156, 1000)
    assert any("faucet.circle.com" in line for line in output)
    assert base.broadcasts == []


async def test_confirmation_is_required_before_the_key_prompt():
    base = FakeBase()
    result, _ = await run_lab(base, answer="yes", read_key=forbidden)
    assert result == {"status": "cancelled"}
    assert base.broadcasts == []


@pytest.mark.parametrize("funds, guidance", [
    ({"usdc": 999_999}, "faucet.circle.com"), ({"eth": 0}, "docs.base.org/get-started/get-funds"),
])
async def test_insufficient_balance_aborts_before_any_broadcast(funds, guidance):
    base = FakeBase(**funds)
    with pytest.raises(ValueError, match=guidance):
        await run_lab(base)
    assert base.broadcasts == []


async def test_key_for_another_address_never_broadcasts():
    base = FakeBase()
    with pytest.raises(ValueError, match="does not match --sender"):
        await run_lab(base, sender=OTHER)
    assert base.broadcasts == []


async def test_full_path_approves_exact_amount_then_burns_once_each():
    base = FakeBase(allowance=0)
    result, output = await run_lab(base)
    assert result["status"] == "interrupted"
    assert [tx["to"] for tx in base.broadcasts] == [USDC, MESSENGER]
    assert base.broadcasts[0]["data"] == lab.approve_calldata(1_000_000)  # exact, not unlimited
    assert base.broadcasts[1]["data"] == lab.burn_calldata(1_000_000, RECIPIENT, 156, 1000)
    assert all(tx["chainId"] == 84532 and tx["type"] == 2 for tx in base.broadcasts)
    assert [tx["nonce"] for tx in base.broadcasts] == [0, 1]
    assert TRANSMITTER not in [tx["to"] for tx in base.broadcasts]
    assert (result["log_index"], result["block_number"]) == (7, BURN_BLOCK)
    assert result["scan_blocks"] == 21 + 1_800
    assert any(f"scan {SENDER} --blocks 1821" in line for line in output)
    assert any("Do NOT mint it any other way" in line for line in output)
    assert all(KEY not in line and KEY[2:] not in line for line in output)


async def test_sufficient_allowance_skips_approve():
    base = FakeBase(allowance=1_000_000)
    result, _ = await run_lab(base)
    assert result["status"] == "interrupted"
    assert [tx["to"] for tx in base.broadcasts] == [MESSENGER]
    assert "approve_tx" not in result


@pytest.mark.parametrize("allowance, step", [(0, "approve"), (1_000_000, "depositForBurn")])
async def test_uncertain_broadcast_reports_hash_and_stops(allowance, step):
    base = FakeBase(allowance=allowance, uncertain=True)
    result, _ = await run_lab(base)
    assert (result["status"], result["step"]) == ("broadcast_unknown", step)
    assert len(result["tx_hash"]) == 66
    assert len(base.broadcasts) == 1


async def test_receipt_timeout_does_not_resend():
    sleeps = []

    async def record(seconds):
        sleeps.append(seconds)

    base = FakeBase(allowance=1_000_000, receipts=False)
    result, _ = await run_lab(base, sleep=record)
    assert result["status"] == "submitted"
    assert len(base.broadcasts) == 1
    assert len(sleeps) == lab.RECEIPT_POLLS - 1


async def test_slow_receipt_request_cannot_extend_the_polling_deadline(monkeypatch):
    class SlowReceiptBase(FakeBase):
        async def call(self, method, params=None):
            if method == "eth_getTransactionReceipt":
                await asyncio.Future()
            return await super().call(method, params)

    monkeypatch.setattr(lab, "RECEIPT_TIMEOUT_SECONDS", 0.01)
    base = SlowReceiptBase(allowance=1_000_000)
    result, _ = await run_lab(base)
    assert (result["status"], result["step"]) == ("submitted", "depositForBurn")
    assert len(base.broadcasts) == 1


async def test_reverted_approve_never_sends_the_burn():
    base = FakeBase(allowance=0, approve_status="0x0")
    result, _ = await run_lab(base)
    assert (result["status"], result["step"]) == ("reverted", "approve")
    assert [tx["to"] for tx in base.broadcasts] == [USDC]


async def test_noncanonical_approval_cannot_release_the_burn():
    base = FakeBase(canonical=False)
    result, _ = await run_lab(base)
    assert (result["status"], result["step"]) == ("receipt_unverified", "approve")
    assert [tx["to"] for tx in base.broadcasts] == [USDC]


@pytest.mark.parametrize("chain,code,match", [
    (84532, "0x", "not Arc Testnet"),
    (5042002, "0x6080", "has contract code"),
])
async def test_recipient_chain_and_code_are_verified(chain, code, match):
    class FakeArc:
        async def call(self, method, params=None):
            if method == "eth_chainId":
                return hex(chain)
            assert (method, params) == ("eth_getCode", [RECIPIENT, "latest"])
            return code

    with pytest.raises(ValueError, match=match):
        await lab.verify_recipient(FakeArc(), RECIPIENT)


def test_getpass_refuses_a_terminal_that_would_echo(monkeypatch):
    def fallback(_prompt):
        warnings.warn("Input would echo", getpass.GetPassWarning, stacklevel=2)
        pytest.fail("Fallback must stop before reading the key")

    monkeypatch.setattr(getpass, "getpass", fallback)
    with pytest.raises(ValueError, match="No key was read"):
        lab.read_hidden_key("Key: ")


async def test_burn_is_not_sent_until_the_approval_is_visible():
    base = FakeBase(allowance=0, approval_lands=False)
    with pytest.raises(ValueError, match="still below"):
        await run_lab(base)
    assert [tx["to"] for tx in base.broadcasts] == [USDC]


@pytest.mark.parametrize("change", [
    {"recipient": pad(OTHER)}, {"hook": b"\x01"}, {"caller": pad(OTHER)}, {"domain": 7},
    {"amount": 999_999}, {"max_fee": 157},
    {"transactionHash": "0x" + "a1" * 32}, {"blockNumber": hex(BURN_BLOCK + 1)},
    {"blockHash": "0x" + "a2" * 32},
])
async def test_post_receipt_decode_mismatch_is_a_failure(change):
    base = FakeBase(allowance=1_000_000, log_changes=change)
    result, output = await run_lab(base)
    assert result["status"] == "burn_unverified"
    assert any("NOT verified" in line for line in output)
    assert len(base.broadcasts) == 1


def test_scan_window_contains_the_burn_and_respects_the_scan_cap():
    assert lab.scan_window(100, 199) == 100 + 1_800
    assert lab.scan_window(100, 99) == 1_801
    assert lab.scan_window(0, 10**9) == 50_000
