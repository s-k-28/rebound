# Implementation validation

November 19, 2026. This records observed checks, not a claim of hackathon readiness.

## Live, read-only checks

- Base Sepolia RPC returned chain ID 84532 and deployed TokenMessengerV2 bytecode.
- Arc Testnet RPC returned chain ID 5042002 and deployed MessageTransmitterV2 bytecode. Both the official `.network` endpoint and `.io` endpoint responded in the backend check.
- A scan from a fresh temporary database discovered a real 1-USDC burn by wallet address, without supplying its hash to the scanner. Source transaction: [0xcfe897ab891b667c9ae538d9d6713c02db167dfd0a058fc6c09313734de0ecbb](https://sepolia.basescan.org/tx/0xcfe897ab891b667c9ae538d9d6713c02db167dfd0a058fc6c09313734de0ecbb), log 4, source block 61,125,511.
- Circle returned a matching signed message with a 0.00013-USDC fee and 0.99987-USDC destination amount. Rebound checked the consumed nonce and matched the on-chain `MessageReceived` event to the original signed message. [Verified Arc receipt](https://explorer.testnet.arc.io/tx/0xd2f74e876f5f9b48ca5836044d0b1fa9723de30672587aacb86b55be6d5bdb00)
- A fixed 200-block scan rediscovered the same transfer from a fresh database. In Chrome, the wallet scan and direct transaction lookup both showed the verified Arc receipt in the inspector.
- The Stream search identified the same completed transfer after a local adapter test fed a real Base receipt through the filter. Live delivery from QuickNode remains pending.
- The observed public Base RPC limited log ranges to 200 blocks and rate-limited bursts. The client now subdivides provider-limited log ranges, spaces log requests, bounds retries to read operations, and cancels sibling scan tasks when a required range fails. A failed scan never becomes a successful empty inbox.

No private key was read and no financial transaction, burn, mint or wallet connection was performed in these checks. A real controlled recovery and destination receipt remain required before claiming end-to-end recovery.

## Correctness review

Tests cover original-recipient and amount binding, message versions, caller restrictions, nonce completion, expiry, malformed responses, canonical source identity, RPC range limits, provider outages, simulation failure, unsigned API preparation, fixture isolation, local key/caller matching, fee caps, confirmation and at-most-one broadcast attempt.

Review found and fixed two edge cases: default Decimal precision could round large uint256 display amounts, and economically identical burns within one source transaction could be assigned the same available attestation. Amount formatting now uses integer arithmetic. Ambiguous source burns are unsupported until exact source-message association is implemented.

## Reproduce the read-only check

```sh
uv run python -m rebound.cli scan 0x41f7337769003aec9b0b20789403f8c741d7faad --blocks 200 --end-block 61125511
uv run python -m rebound.cli inspect 0xcfe897ab891b667c9ae538d9d6713c02db167dfd0a058fc6c09313734de0ecbb
```

Both commands should report 1 USDC sent, 0.99987 USDC received and a completed Arc mint. The source lookup also identifies log 4 for reproducing a completed-state check.

## Runtime checks and limits

The Python/Flet app was opened in Chrome and its actual rendering inspected at desktop and 390px widths. Browser checks caught an invalid expanded control inside a wrapping layout and search losing keyboard focus; both were fixed. Example selection, completed-status filtering, sequential multi-character search and clearing a hidden inspector were exercised against the running app. Search retained focus between separate `4` and `2` keystrokes and correctly showed only the 42-USDC example.

Container instructions and a non-root Dockerfile are provided. The local Docker daemon was unavailable, so the image was not built or run in this session. The Python app itself was started and tested directly with Uvicorn.
