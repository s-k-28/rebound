# Rebound

Rebound finds interrupted USDC transfers from a public wallet address. It checks the source receipt, matches Circle’s message and reads Arc’s nonce state so users can decide whether to wait or finish the existing transfer.

**Initial route: Base Sepolia → Arc Testnet.** Rebound uses a Python/Flet browser interface, a FastAPI backend and a local Python recovery command. A controlled 1-USDC testnet recovery verified address-only discovery, the Arc mint and the refusal to prepare a duplicate mint. [Live recovery evidence](docs/LIVE-RECOVERY.md)

Read the [PRD](docs/PRD.md), [implementation and release plan](docs/superpowers/plans/2026-11-19-rebound.md), [live demo procedure](docs/DEMO.md), [controlled testnet lab](docs/LAB.md) and [QuickNode setup](docs/QUICKNODE.md).

![Rebound transfer workspace showing labeled synthetic examples](docs/images/workspace.jpg)

## QuickNode Streams integration

Rebound uses QuickNode Streams to help users find interrupted USDC transfers with just a public wallet address. A QuickNode Stream reads Base Sepolia and sends only Circle CCTP V2 USDC burn events to Rebound’s authenticated webhook.

**Why QuickNode Streams.** After a lost session, a user has an address and no transaction hash. The alternatives are weaker for this job:

| Approach | Problem for a wallet-only lookup |
|---|---|
| Scan chain history over RPC | Bounded to a block window, slower the further back it reaches, and public RPCs rate-limit or refuse historical log queries. |
| Poll the chain continuously | Rebound would need its own always-on indexer and reorg handling. |
| **QuickNode Streams** | Filtered at the source and pushed as blocks arrive. Rebound stores only matching burns, so a wallet search is a database read, followed by the independent receipt, Circle and Arc checks. |

The filter runs on QuickNode, so Rebound receives matching burns only. Our Stream processed 15,861 blocks and sent Rebound none of them, because none matched the filter. Rebound never had to read or discard those blocks.

When a user selects **Find transfers via QuickNode**, Rebound looks up events associated with their wallet in the persisted Stream index and checks the original burn receipt, Circle’s attestation, and Arc’s on-chain state. This search reads the index only and does not fall back to a source RPC history scan. The user can see whether the transfer is pending, ready to finish, or already complete without looking up the original transaction hash.

```mermaid
flowchart LR
    A["Base Sepolia<br/>USDC burn to Arc"] --> B["QuickNode Stream<br/>CCTP V2 burn filter"]
    B --> C["Signed webhook<br/>POST /api/streams"]
    C --> D[("Persisted<br/>Stream index")]
    D --> E["Wallet inbox<br/>Find transfers via QuickNode"]
    E --> F{"Verify each burn"}
    F --> G["Source receipt"]
    F --> H["Circle attestation"]
    F --> I["Arc nonce state"]
    G --> J["Status shown<br/>to the user"]
    H --> J
    I --> J
```

| Setting | Value | Why it matters |
|---|---|---|
| Network | Base Sepolia (chain 84532) | Circle’s TokenMessengerV2 emits the burn here. |
| Dataset | Block with Receipts (`block_with_receipts`) | The filter reads raw logs from each receipt, so it sees the burn without extra RPC calls. |
| Filter | [`streams/filter.js`](streams/filter.js): successful receipt, TokenMessengerV2 address, `DepositForBurn` topic and native Base Sepolia USDC topic | Only canonical USDC burns are delivered. Blocks with no match are not delivered. The Python decoder still enforces the Arc destination and message fields. |
| Webhook | `POST /api/streams`, QuickNode signature over nonce, timestamp and body, 15 minute window, plain or gzip JSON | Only the holder of the Security token can add to the index. |
| Index | SQLite, with the delivery time and signature status of each burn | The wallet search needs no RPC history scan and can say how each result arrived. |

**Our Stream.** The same facts are shown in the app under **QuickNode walkthrough**, from [`rebound/stream_record.json`](rebound/stream_record.json).

| Field | Value |
|---|---|
| Name | Rebound Base Sepolia burns |
| Stream ID | `c797f5ca-9aa8-4874-b8f1-a291a4066f27` |
| Source | Base Sepolia, Block with Receipts |
| Starts at | Block 61,159,868, no end, batches of 1 |
| Filter | The deployed filter is identical to [`streams/filter.js`](streams/filter.js) |
| Delivery | `POST /api/streams`, signed with the destination’s security token |
| State on 2026-11-20 | **Terminated.** Last block delivered 61,175,720, 15,861 blocks processed, 0 matched, 8 delivery errors |

The Stream stopped at block 61,175,720, one block before the controlled burn at block 61,175,721, so that burn was never delivered. The dashboard does not state a cause. The record is a snapshot of the dashboard. Rebound does not query QuickNode.

How a result is labelled depends on how it arrived:

| Delivery | Label in the inspector | Counts as live QuickNode delivery |
|---|---|---|
| Valid QuickNode signature and matching block hash | **Discovered via QuickNode Streams** | Yes |
| Local shared secret (`REBOUND_STREAM_SECRET`) | Stream endpoint test (no QuickNode signature) | No |
| Demo wallet fixture | Simulated Stream delivery | No |

For a Stream-found transfer the inspector shows **Burn confirmed → Delivered by QuickNode → Transfer status verified**, each tied to that transfer’s evidence: the Base Sepolia block, a link to the original burn, the delivery time, and the source, Circle and Arc checks. QuickNode does not report which blocks it has covered, and a delivered burn proves only its own block, so an empty result is not proof that no transfer exists.

**Validation status.** The checks are tracked separately. Simulations and the local adapter are never counted as a live delivery.

| Check | Result | Evidence | Status |
|---|---|---|---|
| Signed sample delivery | QuickNode **Send Payload** passed | [Webhook setup test](docs/QUICKNODE.md#webhook-setup-test) | Observed |
| Deployed Stream configuration | Base Sepolia, Block with Receipts, webhook to `/api/streams`, and a filter identical to `streams/filter.js` | Read from the QuickNode dashboard, 2026-11-20 | Verified |
| Stream state | Terminated one block before the controlled burn, with 8 delivery errors | Dashboard overview, 2026-11-20 | Observed, needs a restart |
| Filter contract | Constants match the protocol, and the filter keeps only successful canonical USDC burns | `tests/test_quicknode.py` | Verified |
| Simulated delivery | Synthetic example shows the labelled flow | Demo wallet, **Show simulated Stream delivery** | Simulated |
| Local adapter delivery | Real controlled burn (block 61,175,721, log 4) found from the wallet alone; source receipt, Circle attestation and the completed Arc mint matched | Local run, 2026-11-20, labelled as an endpoint test | Verified as adapter test |
| Live Stream delivery of a real burn | Not yet observed. The Stream ended before reaching the burn | Needs the Stream restarted at block 61,175,721 with a reachable destination | Pending |
| Wallet-only discovery from a live delivery | Not yet observed | Follows the live delivery | Pending |
| Completed Arc mint tied to a live delivery | The controlled burn’s mint is complete ([evidence](docs/LIVE-RECOVERY.md)) but is not yet tied to a live delivery | Follows the live delivery | Pending |

See the [QuickNode setup guide](docs/QUICKNODE.md) for configuration and verification steps, and the [recording sequence](docs/DEMO.md#quicknode-streams-walkthrough).

## Run locally

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync
uv run uvicorn rebound.app:app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. The app's UI is written in Python using Flet controls and rendered by Flutter. Application code does not author HTML, CSS or TypeScript. The small JavaScript file under `streams/` is an optional provider-side QuickNode filter, not a web application.

Default public RPCs can be rate-limited or restrict historical log queries. For your own endpoints, copy `.env.example` to `.env`, update it and launch with:

```bash
uv run --env-file .env uvicorn rebound.app:app --host 127.0.0.1 --port 8765
```

Keep `.env`, RPC credentials and signing keys out of Git. `REBOUND_DB` selects a writable SQLite path. Set `REBOUND_STREAM_SECURITY_TOKEN` to validate QuickNode signatures, or use `REBOUND_STREAM_SECRET` for local adapter checks. Ingestion is disabled by default. Production hosting additionally needs HTTPS, durable storage and an abuse/load review.

`/api/health` checks that the app is running. `/api/ready` checks both chain IDs and configured contracts, plus Circle’s Base-to-Arc fee route. A readiness result reports which provider failed. Use the live sample to verify the full attestation and receipt path.

The browser and API share a bounded verification queue. When it fills, the API returns 429 with a retry delay and the inbox shows a retry message. The same time limit covers both queueing and active verification. [OWASP API resource consumption](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/)

## Find and prepare transfers

The browser app scans by public address. It displays the exact source block range and whether results are partial. An empty inbox means no supported transfers were found **within the stated coverage**, not that the wallet has no unfinished transfers anywhere.

The same service is available locally:

```bash
uv run python -m rebound.cli --help
uv run python -m rebound.cli scan "$WALLET_ADDRESS" --blocks 2000
uv run python -m rebound.cli scan "$WALLET_ADDRESS" --blocks 200 --end-block 61125511
uv run python -m rebound.cli inspect "$BURN_TX_HASH"
uv run python -m rebound.cli replay "$BURN_TX_HASH" "$LOG_INDEX"
uv run python -m rebound.cli prepare "$BURN_TX_HASH" "$LOG_INDEX" --caller "$CALLER_ADDRESS"
```

Set those variables to your own public address, the original burn hash and the decimal `DepositForBurn` log index shown in the inbox. `prepare` exports an unsigned destination transaction only after fresh source/message/nonce checks and destination simulation. It does not send anything or request a key.

The local MCP server exposes discovery, inspection, readiness and unsigned preparation to Codex and other MCP clients. [MCP setup](docs/MCP.md)

Use **History options** to end an address scan at a Base Sepolia block or a UTC time. Choose a time after the transfer. Rebound resolves it to the latest available block at or before that time and scans backward by the selected number of blocks. The result reports the exact blocks searched, and transfer status reflects current chain state. When Circle supplies a destination transaction hash for a completed transfer, Rebound links to Arc after checking the successful receipt and its matching CCTP event. [Base block API](https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_getBlockByNumber)

**Refresh this transfer** rechecks the selected source receipt, matching Circle message and Arc nonce without rerunning the wallet scan. **Copy evidence** copies the public transfer identifiers, amounts, verification time and receipt links for a support ticket or demo.

When a completed transfer lacks a receipt link, paste the Arc transaction hash in the inspector and select **Verify Arc receipt**. Rebound checks that the canonical Arc receipt contains the original mint message and nonce, then adds its verified link.

**Copy case link** opens the exact source transaction and event index on the same Rebound deployment. The recipient of the link gets a fresh chain check.

Open `/demo` or search for `0x05af7613449b84f2a02893d35e6cdee479404ce8` to use the synthetic wallet. **Show simulated Stream delivery** opens an example of the Stream discovery workflow. This demo uses local fixture data and cannot prepare a transaction.

For a real Base Sepolia backfill, select **Check a live sample** or scan the controlled run's fixed window. The default 2,000-block window ends at the current head and no longer reaches this burn. The command returns the completed 1-USDC transfer and its verified Arc receipt:

```bash
uv run python -m rebound.cli scan 0x395809d8da4a70145d5060448070a15367df722c --blocks 200 --end-block 61175721
```

The same transfer can be found from a UTC time after the burn. A time without seconds ends at the start of that minute.

```bash
uv run python -m rebound.cli scan 0x395809d8da4a70145d5060448070a15367df722c --blocks 200 --end-time 2026-11-20T06:44Z
```

The [validation notes](docs/VALIDATION.md#reproduce-the-read-only-check) include an independent historical case.

For a completed direct mint, **Replay historical mint** verifies the original receipt, reads the nonce at the previous block and the receipt block, then simulates the same attestation against the previous block. The replay requires an Arc RPC that serves historical state and uses reads only.

### Complete a transfer locally

For an eligible **testnet** transfer, use a throwaway testnet account with destination gas:

```bash
uv run python -m rebound.cli recover "$BURN_TX_HASH" "$LOG_INDEX" --caller "$CALLER_ADDRESS"
```

The command decodes the destination transaction and checks its recipient, original amount, net amount and protocol fee against the review. It shows estimated gas separately. It requires typing `RECOVER` before requesting a hidden local key, checks that the key belongs to the approved caller, and prepares the transfer again immediately before signing. It sends at most once and checks the receipt event against the original message body and nonce. The default maximum gas cost is **0.1 testnet USDC**. Use `--max-gas-usdc` to adjust the local cap.

The private key is never accepted by a web endpoint, written to a file or printed. Python cannot guarantee erasure of secrets from process memory. Use a dedicated test account, not an account holding real assets. If submission times out, the command reports the locally computed transaction hash and does **not** automatically resubmit. Inspect that hash and re-diagnose before another attempt.

Run the CLI and inbox with the same `REBOUND_DB` to see a locally recovered receipt before Circle indexes its destination hash. The CLI saves the verified destination hash and the inbox checks the receipt against the current signed message before linking to it.

The unsigned review shows the caller’s Arc balance and gas budget separately from the CCTP protocol fee. Arc gas uses the native USDC balance in 18-decimal units. The CLI checks the caller’s pending balance before asking for a key and checks it again before signing. [Arc wallet integration guide](https://community.arc.io/public/externals/supporting-arc-in-wallets-one-balance-usdc-fees-and-complete-history-2026-09-10)

## What the states mean

| State | Meaning and next step |
|---|---|
| Source confirming | The source burn needs more confirmations; refresh later |
| Waiting for Circle | Circle has not returned a usable signed message yet; do not burn again |
| Ready to finish | Current checks allow preparation; select the actual caller and revalidate |
| Caller required | The message restricts who can call the destination contract; the recipient is not automatically authorized |
| Attestation expired | The existing burn needs a refreshed attestation. [Circle's re-attestation procedure](https://developers.circle.com/cctp/references/technical-guide#message-body) |
| Forwarded transfer | The burn requested Circle forwarding. Check the original app while Arc’s nonce is unused. |
| Completed | Destination nonce is already consumed; another mint is unnecessary |
| Source needs review | Source evidence no longer matches the canonical receipt/block; inspect rather than sending |
| Outside current support | The route, message, hook or adapter is outside this version's coverage |
| Mint submitted | A destination transaction was submitted; check its receipt before attempting another mint |
| Could not verify | A required source could not be verified; no recovery transaction is prepared. Try again before taking any action |

Circle's attestation status `complete` is **not** proof that the destination mint happened. The original recipient cannot be changed. Recovery here means finishing an existing mint, not refunding or reversing a transfer.

## Architecture

```text
Python Flet interface ─┐
FastAPI endpoints ────┼── RecoveryService ── Base Sepolia RPC
Local Python CLI ────┘          │          ├─ Circle sandbox attestation API
                               │          └─ Arc Testnet RPC
                       SQLite candidates
                               ▲
                 Authenticated Stream ingestion
```

`protocol.py` decodes packed messages and checks invariants. `clients.py` handles RPC and Circle responses. `store.py` indexes candidates. `service.py` discovers and reconciles transfers and prepares unsigned transactions. `ui.py` renders the browser experience. `app.py` mounts the API and Flet. `cli.py` signs recovery transactions locally. A Stream or cached row can suggest a transfer for inspection. The recovery service verifies each transfer independently before preparation.

### Contracts used

| Item | Value |
|---|---|
| Source | Base Sepolia, chain `84532`, CCTP domain `6` |
| Destination | Arc Testnet, chain `5042002`, CCTP domain `26` |
| Source USDC | `0x036CbD53842c5426634e7929541eC2318f3dCF7e` |
| TokenMessengerV2 | `0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA` |
| MessageTransmitterV2 | `0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275` |
| Attestation service | `https://iris-api-sandbox.circle.com` |

These are existing Circle contracts; Rebound has not deployed a new protocol. See the [canonical addresses](https://developers.circle.com/cctp/references/contract-addresses) and [message source](https://github.com/circlefin/evm-cctp-contracts/tree/master/src/messages/v2). Network IDs and deployed bytecode are checked before use. CCTP itself enforces one successful mint per nonce; Rebound does not invent that guarantee. Racing or reverted transactions can still consume gas.

### Economic design and risks

- **Original principal.** Circle’s contracts burn the original USDC on Base Sepolia and mint on Arc to the recipient already encoded in the message. Rebound holds no balance and takes no fee.
- **Costs.** Circle’s protocol fee comes out of the original amount and is bounded by the source burn’s maximum fee. The destination caller pays Arc gas in testnet USDC. The local recovery command prints a separate gas estimate and applies a caller-selected cap.
- **Settlement.** Arc’s consumed nonce prevents another mint of the same message. Competing submissions can spend gas even when only one mint succeeds. Rebound checks the nonce again immediately before preparing a signed recovery.
- **Dependencies.** The flow depends on a canonical confirmed source receipt, Circle’s valid attestation, current Arc chain state and a funded authorized caller. The app bounds discovery to the displayed coverage and checks the original destination caller before simulation.

**UBC track fit.** DeFi and Onchain Applications with Best Use of Arc is the planned entry. The organizer’s guide includes CCTP cross-chain flows in DeFi and allows existing Arc contracts. QuickNode Streams requires a configured live source and destination. [Official hackathon guide](https://docs.google.com/document/d/1QaySCo7u3VxMVNdk3C5Ab3ewc2E46AuLntti-Uzx5EY/edit)

## Checks

```bash
uv run pytest
uv run ruff check .
```

Use `uv run python scripts/watch_prs.py --interval 900 --hours 6` to record pull requests and changed heads every 15 minutes. The watcher uses the authenticated GitHub CLI. Review and merge decisions use the exact tested head.

Use `uv run python scripts/check_preview.py "$PREVIEW_URL"` to check an HTTPS deployment’s liveness, provider readiness, demo entry, verified transfer timeline and historical replay.

Protocol tests exercise message fields, recipient/caller/amount matching, malformed input, nonce completion and expiry. Service tests use HTTP-level fixtures around the real discovery/reconciliation code. CLI tests use a fixed throwaway key and isolated RPC doubles: they verify caller and chain guards, fee caps, confirmation, fresh revalidation, signed sender, exactly one broadcast attempt and receipt evidence. These automated tests do not prove that a real testnet recovery has been executed.

## Coverage and remaining work

- Scans default to 2,000 source blocks and cap at 50,000; provider limits and retained index coverage affect discovery.
- Discovery covers canonical native USDC `DepositForBurn` events from the supported TokenMessengerV2 to Arc. Rebound checks Circle-forwarded mints in read-only mode. For forwarding with an app hook, the verified receipt establishes the CCTP mint and the original app handles downstream actions. Manual recovery is available for eligible hook-free burns. [Circle forwarding formats](https://developers.circle.com/cctp/concepts/forwarding-service#hook-format)
- Source and destination RPCs, Circle indexing and gas availability remain external dependencies.
- No seed recovery, arbitrary crypto recovery, loss insurance, mainnet support or recipient redirection.
- Browser wallet signing, durable hosting and an observed live QuickNode delivery remain open. A temporary HTTPS preview is available for testing. The demo wallet is a synthetic fixture.
- Originality, recurring use, user demand and willingness to pay remain hypotheses rather than validated metrics.

## Final release gates

The [November 20 run](docs/LIVE-RECOVERY.md) records the receipts, screenshots and commands for the first four gates.

- [x] **Controlled source burn**. The testnet lab created a 1-USDC manual transfer to an owned recipient with zero destination caller and preserved the Base receipt.
- [x] **Fresh-session discovery**. An empty inbox found the burn from the wallet address alone and displayed it as ready to finish.
- [x] **Actual destination mint**. The locally signed Arc receipt matched the original nonce and credited 0.99987 USDC to the fixed recipient.
- [x] **Second attempt blocked as completed**. A repeated preparation for the same source hash and log returned the completed-state refusal before signing.
- [ ] **Real QuickNode delivery:** configure an actual Stream, filter and HTTPS destination; preserve its Stream ID, configuration, history range and observed delivery of a known burn into the inbox. A filter file, local fixture or synthetic webhook is insufficient.
- [ ] **Accessible deployment and wallet experience:** verify the public app on desktop and mobile, document the current local-signing handoff, and complete a browser wallet connector before claiming seamless browser signing.
- [ ] **Submission evidence:** publish the tested source revision, setup instructions and an accessible 2–3 minute video. Keep example data labeled and separate from live receipt evidence.

Follow [the detailed demo checklist](docs/DEMO.md). No checked box should imply mainnet recovery, universal history coverage or a guaranteed award.

## Container

```bash
docker build -t rebound .
test -f .env || cp .env.example .env
docker run --rm -p 8765:8765 --env-file .env -v rebound-data:/app/data rebound
```

The container runs the web app as a non-root user. Keep signing keys out of it; local signing is intentionally separate.

Deploy the same container to a public preview with the [Render blueprint](docs/DEPLOY.md).
