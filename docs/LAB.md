# Demo lab: controlled interrupted transfer

`rebound.lab` creates the one thing the live demo needs and the product must never do: a small, manual Base Sepolia → Arc Testnet CCTP V2 burn that stops before the destination mint. It closes the **Controlled source burn** release gate in the README and step 3 of [Controlled testnet preparation](DEMO.md#controlled-testnet-preparation).

It is a lab tool, separate from recovery. Rebound's inbox, API and `rebound.cli` never burn USDC. The lab never calls `receiveMessage` and never reads or writes Rebound's SQLite store, so a fresh Rebound session has to discover the transfer by address.

## Commands

```bash
# Read-only rehearsal: no key, no funds needed
uv run python -m rebound.lab interrupt --amount 1 --recipient "$ARC_RECIPIENT" --dry-run

# Same, also checking a wallet's USDC, ETH gas and allowance
uv run python -m rebound.lab interrupt --amount 1 --recipient "$ARC_RECIPIENT" --sender "$WALLET" --dry-run

# The real burn, from an interactive terminal
uv run python -m rebound.lab interrupt --amount 1 --recipient "$ARC_RECIPIENT"
```

| Option | Default | Meaning |
|---|---|---|
| `--amount` | required | USDC to burn. At most 5, up to 6 decimals. |
| `--recipient` | required | Arc Testnet wallet you control. Contracts and the zero address are refused. |
| `--speed` | `fast` | `fast` = threshold 1000, attestation about 8 seconds. `standard` = threshold 2000, about 15–19 minutes. Live demos use fast. |
| `--rpc` | `BASE_SEPOLIA_RPC` or `https://sepolia.base.org` | Base Sepolia RPC. |
| `--sender` | none | Expected signing address. The key must match it. In `--dry-run` it adds a funding check. |
| `--dry-run` | off | Runs every read-only check and prints the review and calldata. Never asks for a key. |

Attestation times are Circle's published Base averages ([finality and block confirmations](https://developers.circle.com/cctp/concepts/finality-and-block-confirmations)).

## What it does

1. Checks the source RPC is chain `84532` and that TokenMessengerV2 and USDC have deployed code. Checks the recipient has no code on Arc Testnet (chain `5042002`). Set `ARC_TESTNET_RPC` to override the public destination RPC for this read.
2. Fetches Circle's fee quote read-only from `GET /v2/burn/USDC/fees/6/26`. `minimumFee` is in basis points. maxFee = amount × bps / 10,000 × 1.2, rounded up (Circle's fee guide suggests a 10–20% buffer). On November 20 the sandbox quoted 1.3 bps for fast and 0 for standard, so 1 USDC fast gets a maxFee of 0.000156 USDC. Any quote that implies more than 0.01 USDC is refused.
3. Simulates the burn against TokenMessengerV2. The call must either succeed or revert inside the USDC transfer for lack of allowance or balance. Either result shows the selector, domain 26 and the fee are accepted. Any other revert stops the run.
4. Prints the review: from, recipient, amount, maxFee, finality, destination domain 26, a zero `destinationCaller` and no hook.
5. Requires typing `INTERRUPT`. Only then does it read a key with `getpass`. The key is never echoed, logged or saved.
6. Derives the address and checks its USDC and ETH. If either is short it stops before broadcasting and points to the [Circle faucet](https://faucet.circle.com) for USDC or [Base's faucet list](https://docs.base.org/get-started/get-funds) for ETH.
7. If the allowance is below the amount, it sends `approve(TokenMessengerV2, amount)` for the exact amount, never unlimited, and waits for a successful receipt.
8. Sends `depositForBurn(amount, 26, recipient, USDC, 0x0, maxFee, threshold)`, the plain CCTP V2 function with selector `0x8e0250ee`. Each step is simulated with `eth_call`, gas-estimated, signed with EIP-1559 fees and broadcast **at most once**. The 0.001 ETH cap applies to execution gas per transaction. Base charges a separate L1 data fee. [OP Stack transaction fees](https://docs.optimism.io/op-stack/transactions/fees)
9. Matches the receipt to the canonical Base block and decodes `DepositForBurn` with `protocol.decode_burn`. The log must match the receipt's transaction and block, as well as the reviewed amount, recipient, depositor, domain 26, zero caller, empty hook, maxFee and threshold. A verification failure stops the command and prints the transaction hash.
10. Prints the source hash, log index, block, Basescan link and the scan commands for a fresh Rebound session.

If a broadcast times out or the RPC's answer is unclear, the lab prints the locally computed hash and stops. It never resends. Check that hash before running again. Once an approval confirms, a rerun skips the approval step.

## Plugging into the recording

Run the lab **before** recording, within 24 hours of the session. Circle encodes an expiration block about 24 hours ahead in the attested message ([technical guide](https://developers.circle.com/cctp/technical-guide)). After that, Rebound shows Expired until the message is re-attested.

1. **Fund.** Use a throwaway Base Sepolia wallet with at least the burn amount in USDC and a little ETH. The Arc caller also needs Arc Testnet USDC for destination gas. Rehearse with `--sender "$WALLET" --dry-run`.
2. **Interrupt.** Run the lab without `--dry-run`. Keep the printed source hash, log index and block for the evidence list in DEMO.md, but do not load them into Rebound.
3. **Fresh scan.** Start Rebound with a new `REBOUND_DB`, enter only the wallet address and scan with the `--blocks N` printed by the lab. In the browser, set the same window under History options. The printed fixed window (`--blocks 200 --end-block <burn block + 100>`) reproduces the same range later.
4. **Finish.** Wait for Ready, which takes 12 source confirmations plus Circle's attestation. Prepare from the caller, then run `uv run python -m rebound.cli recover <hash> <log index> --caller <caller>`.
5. **Refresh.** The transfer shows Completed and links the Arc receipt.
6. **Second attempt refused.** Run `prepare` again for the same hash and log index. It must refuse with the completed state.

This maps to steps 1–7 of DEMO.md's recording sequence. The lab only produces step 1's burn.

## Limits

- **Testnet only:** Base Sepolia → Arc Testnet with canonical USDC. Not for mainnet keys or real funds.
- **Anyone can mint it:** a zero `destinationCaller` lets anyone submit the mint once Circle attests. Nothing on testnet is expected to, but if someone does, Rebound correctly shows Completed. Do not mint it yourself any other way.
- **Verified live:** on November 20 this tool broadcast a 1 USDC Base Sepolia burn that Rebound then found by wallet address and finished on Arc ([controlled recovery](LIVE-RECOVERY.md)). The automated tests still use RPC doubles and a fixed fixture key.
