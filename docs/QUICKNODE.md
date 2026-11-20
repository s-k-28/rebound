# QuickNode Streams adapter

The repository includes a provider-side filter and an authenticated candidate-ingestion endpoint. **No live Stream is claimed to be deployed or bounty-qualified.** Configure and verify a real Stream before selecting that bounty.

## Configuration

1. In QuickNode Streams, select **Base Sepolia** and the **Block with Receipts** dataset (`block_with_receipts`). The [supported networks list](https://www.quicknode.com/docs/streams/supported-chains) lists this combination. Confirm access and current history limits for your account.
2. Paste `streams/filter.js` into the JavaScript filter editor. QuickNode requires an entry point called `main(stream)`; it receives `stream.data` and `stream.metadata`. The dataset contains entries with `receipts`, each containing raw EVM logs. This small JavaScript file runs on QuickNode; the Rebound app remains Python.
3. Test against a block containing a known native-USDC CCTP V2 burn. The filter selects the canonical TokenMessengerV2, DepositForBurn topic and native Base Sepolia USDC topic, retaining the full raw log identity. The Python decoder then enforces destination domain and message invariants. Do not turn provider input into a “ready” transaction directly.
4. Choose an explicit historical start/end range for backfill, then a tip-following feed if needed. Record these ranges. Enable appropriate confirmation distance and reorg restreaming. Follow the current batch-size restrictions shown by QuickNode.
5. Set the webhook destination to `https://YOUR_DEPLOYMENT/api/streams`. Copy the destination's **Security token** into the server environment as `REBOUND_STREAM_SECURITY_TOKEN`. Rebound verifies the signature over the nonce, timestamp and original JSON bytes. It accepts timestamps within 15 minutes of the server clock and supports plain or gzip-compressed JSON. [QuickNode signature guide](https://www.quicknode.com/guides/quicknode-products/streams/validating-incoming-streams-webhook-messages)
6. Verify the provider's actual delivery envelope. The filter returns `{ "logs": [...] }`, and the receiver accepts this object or the QuickNode wrapper under `data`. Compare the received event with its canonical source receipt.

For local adapter checks, set `REBOUND_STREAM_SECRET` and send it in `x-rebound-secret`. When `REBOUND_STREAM_SECURITY_TOKEN` is set, the endpoint requires a QuickNode signature.

The receiver stores event candidates in SQLite. Duplicate delivery does not create another recovery action. Canonical source receipt/block, the Circle message, destination nonce and actual caller are independently rechecked before preparation. Reorged or removed candidates cannot authorize a mint.

Use **Search Stream index** in the inbox or `python -m rebound.cli stream-index WALLET` after delivery. This search reads candidates received through the webhook and verifies them with source receipts and destination chain state. It does not run a source `eth_getLogs` history scan. The inspector shows **Discovered via · Stream delivery** when the indexed event matches the delivered block hash. The coverage note applies even when no transfers are found.

## Record for the README and video

| Field | Record after actual setup |
|---|---|
| Network | Base Sepolia |
| Dataset | Block with Receipts |
| Filter | Exact deployed `streams/filter.js` revision |
| Destination | HTTPS webhook path, with credentials redacted |
| Stream ID | Actual ID returned by QuickNode |
| History | Backfill start/end and current tip coverage |
| Delivery evidence | Known source burn arriving through the Stream and visible in the inbox |
| Recovery role | Discovery feed; canonical verification remains a separate service step |

An HTTP request with a fixture proves the adapter works; it does not prove a QuickNode Stream exists. Streams are billed for blocks processed even if a filter returns no matching logs.

Official references: [filters](https://www.quicknode.com/docs/streams/filters), [EVM dataset schemas](https://www.quicknode.com/docs/streams/data-sources/ethereum-evm), [create Stream](https://www.quicknode.com/docs/streams/rest-api/create-stream), [backfill and reorgs](https://www.quicknode.com/docs/streams/backfilling).
