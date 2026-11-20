# QuickNode Streams adapter

The repository includes a provider-side filter and an authenticated candidate-ingestion endpoint. **No live Stream is claimed to be deployed or bounty-qualified.** Configure and verify a real Stream before selecting that bounty.

## Configuration

1. In QuickNode Streams, select **Base Sepolia** and the **Block with Receipts** dataset (`block_with_receipts`). The [supported networks list](https://www.quicknode.com/docs/streams/supported-chains) lists this combination. Confirm access and current history limits for your account.
2. Paste `streams/filter.js` into the JavaScript filter editor. QuickNode requires an entry point called `main(stream)`; it receives `stream.data` and `stream.metadata`. The dataset contains entries with `receipts`, each containing raw EVM logs. This small JavaScript file runs on QuickNode; the Rebound app remains Python.
3. Test against a block containing a known native-USDC CCTP V2 burn. The filter selects the canonical TokenMessengerV2, DepositForBurn topic and native Base Sepolia USDC topic, retaining the full raw log identity. The Python decoder then enforces destination domain and message invariants. Do not turn provider input into a “ready” transaction directly.
4. Choose an explicit historical start/end range for backfill, then a tip-following feed if needed. Record these ranges. Enable appropriate confirmation distance and reorg restreaming. Follow the current batch-size restrictions shown by QuickNode.
5. Expose the app over HTTPS. In the server environment set a long random `REBOUND_STREAM_SECRET`. The webhook destination is `https://YOUR_DEPLOYMENT/api/streams`, with custom headers `Content-Type: application/json` and `x-rebound-secret: YOUR_SECRET`. Do not commit the secret or put it in a URL.
6. Use uncompressed JSON for the initial adapter. Test actual delivery shape: the filter returns `{ "logs": [...] }`; QuickNode delivery may wrap the result under `data` with `metadata`. Confirm the receiving API accepts the delivered envelope, not merely a hand-crafted local payload.

The receiver stores event candidates in SQLite. Duplicate delivery does not create another recovery action. Canonical source receipt/block, the Circle message, destination nonce and actual caller are independently rechecked before preparation. Reorged or removed candidates cannot authorize a mint.

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
