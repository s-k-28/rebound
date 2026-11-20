# QuickNode Streams adapter

The repository includes a provider-side filter and an authenticated candidate-ingestion endpoint. QuickNode's signed sample passed during setup. Independent observation of a real burn arriving from the configured Stream is still pending; payload authentication alone does not complete that check.

Public preview users need no credentials, wallet connection or QuickNode account. These configuration steps are for the server operator. Never enter a Security token, RPC credential, seed phrase or private key in the public app.

## Configuration

1. In QuickNode Streams, select **Base Sepolia** and the **Block with Receipts** dataset (`block_with_receipts`). The [supported networks list](https://www.quicknode.com/docs/streams/supported-chains) lists this combination. Confirm access and current history limits for your account.
2. Paste `streams/filter.js` into the JavaScript filter editor. QuickNode requires an entry point called `main(stream)`; it receives `stream.data` and `stream.metadata`. The dataset contains entries with `receipts`, each containing raw EVM logs. This small JavaScript file runs on QuickNode; the Rebound app remains Python.
3. Test against a block containing a known native-USDC CCTP V2 burn. The filter selects the canonical TokenMessengerV2, DepositForBurn topic and native Base Sepolia USDC topic, retaining the full raw log identity. The Python decoder then enforces destination domain and message invariants. Do not turn provider input into a “ready” transaction directly.
4. Choose an explicit historical start/end range for backfill, then a tip-following feed if needed. Record these ranges. Enable appropriate confirmation distance and reorg restreaming. Follow the current batch-size restrictions shown by QuickNode.
5. Set the webhook destination to `https://YOUR_DEPLOYMENT/api/streams`. Copy the destination's **Security token** into the server environment as `REBOUND_STREAM_SECURITY_TOKEN`. Rebound verifies the signature over the nonce, timestamp and original JSON bytes. It accepts timestamps within 15 minutes of the server clock and supports plain or gzip-compressed JSON. [QuickNode signature guide](https://www.quicknode.com/guides/quicknode-products/streams/validating-incoming-streams-webhook-messages)
6. Verify the provider's actual delivery envelope. The filter returns `{ "logs": [...] }`, and the receiver accepts this object or the QuickNode wrapper under `data`. Compare the received event with its canonical source receipt.

For local adapter checks, set `REBOUND_STREAM_SECRET` and send it in `x-rebound-secret`. When `REBOUND_STREAM_SECURITY_TOKEN` is set, the endpoint requires a valid HMAC using that token. Run local fixtures and signed replays against a separate local database, not the public preview's index. A locally signed replay can authenticate successfully and still supplies no independent evidence of provider delivery.

The receiver stores event candidates in SQLite. Duplicate delivery does not create another recovery action. Canonical source receipt/block, the Circle message, destination nonce and actual caller are independently rechecked before preparation. Reorged or removed candidates cannot authorize a mint.

Use **Search Stream index** in the inbox or `uv run python -m rebound.cli stream-index "$WALLET"` after delivery. This search reads candidates received through the webhook and verifies them with source receipts and destination chain state. It does not run a source `eth_getLogs` history scan. **Authenticated Stream payload** means that the configured token validated the payload and its block hash matched the log. It does not independently prove that QuickNode sent it: the token holder can also create a local signed replay. A burn accepted with the local shared secret is labelled **Stream endpoint (no QuickNode signature)**. Neither authentication method alone completes the provider-observation gate. QuickNode does not report block coverage to Rebound, so an empty result is not proof that no transfer exists.

The inbox also has a **QuickNode walkthrough** dialog. It fills the walkthrough wallet and separates the recorded signed sample, synthetic examples, authenticated endpoint payloads, wallet-only lookup and completed Arc mint. Independently observed provider delivery remains a separate Pending check until a dashboard delivery record is matched to the endpoint payload and canonical source receipt. The walkthrough wallet defaults to the throwaway testnet wallet in [LIVE-RECOVERY.md](LIVE-RECOVERY.md). Set `REBOUND_QUICKNODE_DEMO_WALLET` to a public address to use another.

Use Base Sepolia block `61175721` and wallet `0x395809d8da4a70145d5060448070a15367df722c` for a focused backfill check. The expected match is source log `4`. Check `/api/ready` for `stream_ingestion_configured` on the deployment receiving the webhook. [Controlled recovery evidence](LIVE-RECOVERY.md)

## Record for the README and video

The author recorded the following dashboard snapshot on **2026-11-20**. It is preserved as reported evidence; the current dashboard state, deployed settings and delivery outcomes have not been independently verified here.

| Field | Author-reported value |
|---|---|
| Network / dataset | Base Sepolia / Block with Receipts (`block_with_receipts`) |
| Filter | Reported identical to `streams/filter.js` in the dashboard editor |
| Destination | Webhook, `POST /api/streams`, no payload compression, 30 second timeout; credentials omitted |
| Stream ID | `c797f5ca-9aa8-4874-b8f1-a291a4066f27` (Rebound Base Sepolia burns) |
| History | Starts at block 61,159,868 with no end; batch size 1; USA East; Free plan |
| Recorded state | **Terminated**; last reported block 61,175,720; 15,861 blocks processed; 0 blocks sent; 8 errors |
| Delivery evidence | Pending; no successful provider delivery record for the burn at 61,175,721 |
| Recovery role | Discovery hints; canonical verification remains a separate service step |

The machine-readable copy is [`rebound/stream_record.json`](../rebound/stream_record.json). The app shows this bundled snapshot under **QuickNode walkthrough**, not a live QuickNode status query. Preserve the date and provenance when updating it. A zero blocks-sent count does not by itself establish that no burns matched the filter or explain the errors.

## Why this Stream

A user who lost their session may have an address and no transaction hash. RPC discovery is bounded to a selected block window and provider retention or query limits. A configured Stream can filter incoming blocks for successful receipts containing a `DepositForBurn` from Circle's TokenMessengerV2 for native Base Sepolia USDC. Rebound can persist those events and later look them up by wallet. Lookup is a database read, followed by independent source receipt, Circle attestation and Arc requests; verification time depends on those providers. The Stream is a discovery feed and never authorizes recovery by itself.

## Showing the integration without a live delivery

A recording does not need a live Stream. Start the app with `REBOUND_STREAM_SECRET` set and run `REBOUND_STREAM_SECRET=... uv run python scripts/seed_demo_delivery.py`. It delivers the documented controlled burn through the real `/api/streams` endpoint with the shared secret. The walkthrough wallet then finds it from the address alone, and the app labels it **Stream endpoint (no QuickNode signature)**, because it is an endpoint test and not a QuickNode delivery. Say so in the recording. Never describe it as live.

## Observed state and how to finish the live check

The dated author snapshot reports **Terminated**, 8 errors, 0 blocks sent and last reported block 61,175,720, before the controlled burn. It does not establish a cause or the current state. The author reported an empty Logs tab and a Codespace destination; neither constitutes successful provider delivery evidence.

The remaining gate requires an operator to independently observe a successful provider delivery record, match its event identity to the received payload and canonical source receipt, then find that transfer using **Search Stream index** with only its public wallet address. **Authenticated Stream payload** proves configured-token authentication and a matching block hash, including for a locally signed replay; it does not complete this gate. Publish a redacted evidence record with its observation time, and update the snapshot and README only after this check.

An HTTP request with a fixture proves the adapter works; it does not prove a QuickNode Stream exists. Streams are billed for blocks processed even if a filter returns no matching logs.

Official references: [filters](https://www.quicknode.com/docs/streams/filters), [EVM dataset schemas](https://www.quicknode.com/docs/streams/data-sources/ethereum-evm), [create Stream](https://www.quicknode.com/docs/streams/rest-api/create-stream), [backfill and reorgs](https://www.quicknode.com/docs/streams/backfilling).

## Webhook setup test

QuickNode's **Check Connection** returned `401 Invalid or stale Stream signature`.
The separate **Send Payload** test reported **Stream test successful**.
In a separate local adapter check, the real controlled Base receipt passed through the filter, a locally signed gzip payload and wallet-only Stream index lookup. Rebound linked the matching canonical Arc receipt. This proves the adapter path, not provider delivery.
Live Base Sepolia burn delivery still needs an independently observed provider dashboard record matched to the endpoint payload, canonical source receipt and **Search Stream index** result.
