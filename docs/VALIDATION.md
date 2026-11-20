# Initial implementation validation

November 19, 2026. This records observed checks, not a claim of hackathon readiness.

## Live, read-only checks

- Base Sepolia RPC returned chain ID 84532 and deployed TokenMessengerV2 bytecode.
- Arc Testnet RPC returned chain ID 5042002 and deployed MessageTransmitterV2 bytecode. Both the official `.network` endpoint and `.io` endpoint responded in the backend check.
- A scan from a fresh temporary database discovered a real 1-USDC burn by wallet address, without supplying its hash to the scanner. Source transaction: [0xcfe897ab891b667c9ae538d9d6713c02db167dfd0a058fc6c09313734de0ecbb](https://sepolia.basescan.org/tx/0xcfe897ab891b667c9ae538d9d6713c02db167dfd0a058fc6c09313734de0ecbb), log 4, source block 61,125,511.
- Circle returned a matching signed message with a 0.00013-USDC fee and 0.99987-USDC destination amount. Arc had already consumed that message's nonce; Rebound correctly classified it as **completed**, not recoverable.
- The observed public Base RPC limited log ranges to 200 blocks and rate-limited bursts. The client now subdivides provider-limited log ranges, spaces log requests, bounds retries to read operations, and cancels sibling scan tasks when a required range fails. A failed scan never becomes a successful empty inbox.

No private key was read and no financial transaction, burn, mint or wallet connection was performed in these checks. A real controlled recovery and destination receipt remain required before claiming end-to-end recovery.

## Correctness review

Tests cover original-recipient and amount binding, message versions, caller restrictions, nonce completion, expiry, malformed responses, canonical source identity, RPC range limits, provider outages, simulation failure, unsigned API preparation, fixture isolation, local key/caller matching, fee caps, confirmation and at-most-one broadcast attempt.

Review found and fixed two edge cases: default Decimal precision could round large uint256 display amounts, and economically identical burns within one source transaction could be assigned the same available attestation. Amount formatting now uses integer arithmetic. Ambiguous source burns are unsupported until exact source-message association is implemented.

The current test count and last verification commands are reported with the final code update; they are intentionally not frozen into this document while the UI is being revised.
