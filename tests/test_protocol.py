"""Protocol invariants: failures here can prepare a wrong financial transaction."""
from dataclasses import replace

import pytest
from eth_abi import encode
from eth_utils import keccak

from rebound import protocol as p

WALLET = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
MESSENGER = "0x8fe6b999dc680ccfdd5bf7eb0974218be2542daa"
TOKEN = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"


def word_address(value):
    return bytes.fromhex(value[2:]).rjust(32, b"\0")


def message_hex(*, caller="0x" + "00" * 20, recipient=WALLET, expiration=1000, fee=500):
    # Independent fixture encoded from Circle's published packed layout.
    head = (1).to_bytes(4, "big") + (6).to_bytes(4, "big") + (26).to_bytes(4, "big")
    head += bytes.fromhex("ab" * 32) + word_address(MESSENGER) * 2
    head += word_address(caller) + (2000).to_bytes(4, "big") * 2
    body = (1).to_bytes(4, "big") + word_address(TOKEN) + word_address(recipient)
    body += (128_500_000).to_bytes(32, "big") + word_address(WALLET)
    body += (1000).to_bytes(32, "big") + fee.to_bytes(32, "big")
    body += expiration.to_bytes(32, "big")
    return "0x" + (head + body).hex()


def burn_log(*, caller="0x" + "00" * 20, recipient=WALLET, amount=128_500_000):
    signature = "DepositForBurn(address,uint256,address,bytes32,uint32,bytes32,bytes32,uint256,uint32,bytes)"
    return {
        "address": MESSENGER,
        "topics": ["0x" + keccak(text=signature).hex(), "0x" + word_address(TOKEN).hex(),
                   "0x" + word_address(WALLET).hex(), "0x" + (2000).to_bytes(32, "big").hex()],
        "data": "0x" + encode(["uint256", "bytes32", "uint32", "bytes32", "bytes32", "uint256", "bytes"],
            [amount, word_address(recipient), 26, word_address(MESSENGER), word_address(caller), 1000, b""]).hex(),
        "transactionHash": "0x" + "cd" * 32,
        "blockHash": "0x" + "ef" * 32,
        "blockNumber": "0x64", "logIndex": "0x3", "removed": False,
    }


def test_decode_packed_message_uses_cctp_v2_offsets():
    m = p.decode_message(message_hex())
    assert (m.source_domain, m.destination_domain, m.amount, m.fee) == (6, 26, 128_500_000, 500)
    assert m.mint_recipient == WALLET
    assert m.nonce == "0x" + "ab" * 32
    assert m.expires_at == 1000


def test_decode_burn_preserves_transaction_and_log_identity():
    burn = p.decode_burn(burn_log())
    assert burn.amount == 128_500_000
    assert burn.log_index == 3
    assert burn.block_number == 100
    assert burn.mint_recipient == WALLET
    assert burn.depositor == WALLET


@pytest.mark.parametrize("message", ["0x", "0x01", "0xzz", "1234"])
def test_malformed_messages_are_rejected(message):
    with pytest.raises(ValueError):
        p.decode_message(message)


def test_non_evm_address_padding_is_not_truncated_silently():
    raw = bytearray.fromhex(message_hex()[2:])
    raw[148 + 36] = 1
    with pytest.raises(ValueError, match="padding"):
        p.decode_message("0x" + raw.hex())


@pytest.mark.parametrize("field,value", [("amount", 1), ("mint_recipient", OTHER),
    ("depositor", OTHER), ("destination_domain", 0), ("caller", OTHER), ("max_fee", 999)])
def test_message_must_match_the_exact_burn(field, value):
    burn = replace(p.decode_burn(burn_log()), **{field: value})
    with pytest.raises(ValueError):
        p.validate_match(burn, p.decode_message(message_hex()))


def test_fee_larger_than_original_maximum_is_rejected():
    with pytest.raises(ValueError, match="fee"):
        p.validate_match(p.decode_burn(burn_log()), p.decode_message(message_hex(fee=1001)))


def test_completed_nonce_wins_over_expired_attestation():
    m = p.decode_message(message_hex(expiration=99))
    assert p.classify(m, nonce_used=True, destination_block=100) == "completed"


def test_expiration_boundary_and_zero_expiration():
    assert p.classify(p.decode_message(message_hex(expiration=100)), nonce_used=False, destination_block=100) == "expired"
    assert p.classify(p.decode_message(message_hex(expiration=0)), nonce_used=False, destination_block=100) == "ready"


def test_restricted_caller_does_not_authorize_recipient_automatically():
    m = p.decode_message(message_hex(caller=OTHER))
    assert p.classify(m, nonce_used=False, destination_block=1, caller=WALLET) == "caller_required"
    assert p.classify(m, nonce_used=False, destination_block=1, caller=OTHER) == "ready"


def test_removed_or_wrong_contract_logs_are_rejected():
    for change in [{"removed": True}, {"address": OTHER}, {"topics": []}]:
        with pytest.raises(ValueError):
            p.decode_burn(burn_log() | change)


def test_receive_calldata_contains_only_original_message_and_attestation():
    from eth_abi import decode
    message = message_hex()
    attestation = "0x" + "11" * 65
    payload = p.receive_calldata(message, attestation)
    assert payload[:10] == "0x57ecfd28"
    args = decode(["bytes", "bytes"], bytes.fromhex(payload[10:]))
    assert args == (bytes.fromhex(message[2:]), bytes.fromhex(attestation[2:]))


def test_amount_formatting_never_uses_binary_float():
    assert p.usdc(128_500_001) == "128.500001"
    assert p.usdc(1) == "0.000001"
