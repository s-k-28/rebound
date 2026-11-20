# Rebound submission draft

Answers follow the order of the Devpost form. Every claim links to evidence in this repository or on a public explorer.

## Elevator pitch

Rebound finds interrupted USDC transfers by wallet address, verifies their status on Arc, and helps complete the original mint.

## What did you build?

Rebound is a recovery inbox for USDC transfers from Base Sepolia to Arc Testnet. You enter a public wallet address. Rebound finds supported CCTP burns where that address deposited the USDC or is the mint recipient, matches each one to Circle's signed message, and checks the mint on Arc. Each transfer gets one plain status, such as Source confirming, Waiting for Circle, Ready to finish, Caller required, Attestation expired or Completed. [Status vocabulary](DESIGN.md#status-vocabulary)

Transfers are listed in a ledger. Selecting one opens an inspector with three proof steps: the Base burn, Circle's attestation and the Arc mint. Each step shows its evidence, such as block numbers, hashes and nonces. The burn and mint steps also show the time recorded on that chain. For the controlled transfer it reads "Minted 06:45:12 UTC, 99 s after the burn". On a 390 px phone the rows stack and the inspector opens as a full-width sheet. [Inspector](DESIGN.md#inspector) · [Phone layout](DESIGN.md#mobile-390-px-portrait) · [Gallery](images/devpost/README.md)

For a ready transfer, Rebound prepares the one transaction that finishes it. Circle's signature fixes the recipient, so that transaction can only pay the person the sender originally chose. The same checks are available in the browser inbox, in a local command line tool and, for AI agents, through a read-only MCP server ([agent tools](MCP.md)).

## What problem does it solve?

CCTP moves USDC in three steps. The source chain burns it, Circle signs an attestation, and someone submits that attestation on the destination chain to mint it. The app that started the transfer usually does the last step. If the tab closes, the app crashes or an agent loses its session after the burn, the USDC is gone from Base and not yet on Arc.

An eligible caller can complete a supported transfer when Circle's attestation is available and still valid. Finding that transfer is the hard part when you only have an address. For example, Circle's App Kit resumes from a saved bridge result. Starting a second transfer creates a separate burn and leaves the original mint unfinished. [Circle App Kit recovery](https://community.arc.io/public/blogs/app-kit-faqs-2026-07-07#bridge)

## How does it work?

1. **Discovery.** Rebound scans `DepositForBurn` events from Circle's TokenMessengerV2 on Base Sepolia where the wallet is the depositor or the recipient. In the inbox a search covers 200, 2,000, 10,000 or 50,000 blocks. It ends at the latest block by default. **History options** can end it at a chosen block or at a UTC time, which Rebound resolves to the latest block at or before that time. The inbox states the exact block range searched. Base Sepolia produced about 5 blocks per second on November 20, so the default 2,000 blocks cover about 7 minutes. A QuickNode Stream index can supply the same events without a scan. [History options](../README.md#find-and-prepare-transfers) · [Block rate](DEMO.md)
2. **Verification.** Rebound re-fetches the canonical source receipt and checks that the transaction is still at its position in the canonical Base block. It pulls the message from Circle's attestation API and compares amount, recipient, caller restriction, fee, finality and hook against the original burn. It rejects any mismatch or ambiguity.
3. **Destination state.** It reads `usedNonces` on Arc's MessageTransmitterV2 and the message's expiry block. A used nonce means the transfer is completed, whatever Circle's status says. The burn time comes from the canonical Base block and the mint time from the verified Arc receipt's block.
4. **Waiting.** While a selected live transfer is Source confirming or Waiting for Circle, the inspector checks it again on a 10-second countdown, for up to 10 minutes. A Stop link ends the checks. [Inspector](DESIGN.md#inspector)
5. **Finishing.** For a ready transfer, **Review recovery** rechecks the transfer, simulates `receiveMessage(message, attestation)` from the caller you enter and shows the unsigned transaction with that caller's Arc balance and gas budget. The browser does not sign. The local command displays the fixed recipient, protocol fee and gas cap, then broadcasts once after explicit confirmation and checks the Arc receipt's `MessageReceived` event against the original nonce and message body. [Local recovery](../README.md#complete-a-transfer-locally)

Provider failures show as unavailable and never as an empty inbox. Rebound holds no funds, takes no fee and never burns again.

**Live proof (November 20, testnet).** We burned 1 USDC on Base Sepolia and deliberately skipped the mint ([source burn](https://sepolia.basescan.org/tx/0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23)). A fresh Rebound session found it from the wallet address alone and marked it ready. Rebound then finished it on Arc ([destination mint](https://explorer.testnet.arc.io/tx/0x92020b4554591c5afc4ae31aff0a8906fc19d1a17f2bd1773e1592762ea0a0a5)), 99 seconds after the burn, and refused a second attempt. The inspector shows the same times from the chains: burned 06:43:33 UTC, minted 06:45:12 UTC. A lookup ending at 06:44 UTC finds this burn again from the address. [Full record](LIVE-RECOVERY.md)

Rebound uses Circle CCTP V2 and USDC, Arc Testnet, Circle's attestation API, Base Sepolia, Python, FastAPI, Flet and SQLite. A QuickNode Stream (`c797f5ca-9aa8-4874-b8f1-a291a4066f27`) filters Base Sepolia for Circle CCTP V2 USDC burns and delivers them to a signed webhook, so a wallet search is a database read instead of a history scan. [Why this Stream](QUICKNODE.md#why-this-stream) QuickNode's signed sample delivery passed. The Stream was terminated one block before the controlled burn, so discovery of a live burn through it is pending. [QuickNode status](QUICKNODE.md#observed-state-and-how-to-finish-the-live-check) Rebound uses Circle's deployed TokenMessengerV2 and MessageTransmitterV2. [Contract addresses](../README.md#contracts-used)

## What makes it unique?

- **It starts from an address.** You do not need a transaction hash, a bridge link or the original app. To look further back, end the search at a Base Sepolia block or a UTC time.
- **It cross-checks three sources.** Rebound combines the canonical Base receipt, Circle's matching attestation and Arc's current nonce state before showing a transfer as ready or completed.
- **It shows when the burn and mint happened.** The times come from the Base and Arc blocks. For waiting and ready transfers, the inspector shows the time since the burn.
- **It completes the original mint.** The attestation fixes the recipient, and Arc allows one successful mint per CCTP nonce. Users can share a case pinned to the exact source event so someone else can recheck it.
- **Agents can use it.** The read-only MCP tools return a `next_step` for each transfer and produce an unsigned preparation for eligible callers. `find_unfinished_transfers` accepts the same `end_block` or `end_time` cutoff as the inbox.

## What would you build next?

- **More routes.** Confirm token and contract addresses, message formats, finality rules and fee handling for each new network before enabling it.
- **Wallet-based signing.** Give users a browser-wallet handoff and verify the exact Arc receipt after the wallet submits it.
- **Support integrations.** An embeddable status check, so a wallet or exchange support team can answer a "where is my USDC" ticket from a customer's address.
- **Continuous history.** Make the QuickNode Stream index the default discovery path instead of bounded scans.
- **Expired messages.** Request Circle re-attestation from inside the inbox when an attestation has expired.

## Gallery

Upload in this order. Images 1 to 5 and 7 use live testnet data. Image 6 is synthetic, and the app labels it as an example. [Capture notes](images/devpost/README.md)

| # | Image | Caption |
|---|---|---|
| 1 | [Ledger and inspector](images/devpost/01-ledger-inspector-completed.jpg) | Check a live sample finds the controlled 1 USDC transfer, burned at 06:43:33 UTC and minted on Arc 99 s later. |
| 2 | [Ready to finish](images/devpost/02-ready-to-finish.jpg) | A case link shows the proof, fixed recipient and amounts for a real 1 USDC burn that was ready at capture. |
| 3 | [Review recovery](images/devpost/03-review-recovery.jpg) | Review recovery rechecks the transfer, simulates the Arc mint and shows the unsigned transaction and gas budget without signing it. |
| 4 | [History by UTC time](images/devpost/04-history-utc-cutoff.jpg) | A wallet lookup ending at 06:44 UTC scans 200 Base Sepolia blocks and finds the controlled transfer. |
| 5 | [Phone](images/devpost/05-phone-inspector.jpg) | At 390 px, ledger rows stack and the inspector opens the same case as a full-width sheet. |
| 6 | [Examples](images/devpost/06-examples-synthetic.jpg) | Example mode shows Ready to finish, Waiting for Circle, Caller required and Completed with synthetic transfers that cannot be signed. |
| 7 | [Recover command](images/devpost/07-recover-terminal.jpg) | Typeset terminal output from the November 20 controlled run, ending with the confirmed Arc mint. |

## Entry details

| Field | Draft |
|---|---|
| Main track | DeFi and Onchain Applications |
| Chain award | Best Use of Arc |
| Repository | [s-k-28/rebound](https://github.com/s-k-28/rebound) |
| Contracts and economics | [Technical README](../README.md#contracts-used) |
| Demo evidence | [Controlled recovery](LIVE-RECOVERY.md) and [independent read-only replay](VALIDATION.md#reproduce-the-read-only-check) |
| Agent tools | [MCP server](MCP.md) |
| Gallery | [Seven images with captions](images/devpost/README.md) |
| Video | Pending verification |
| QuickNode Streams | [Why and Stream record](QUICKNODE.md#why-this-stream). Live delivery pending |
