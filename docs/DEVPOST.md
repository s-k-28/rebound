# Rebound submission draft

Answers follow the order of the Devpost form. Every claim links to evidence in this repository or on a public explorer.

## Elevator pitch

Rebound finds USDC stuck halfway through a Circle CCTP transfer using only your wallet address, shows exactly where it stopped, and finishes the original mint on Arc.

## What did you build?

Rebound is a recovery inbox for USDC that was burned on Base Sepolia but never minted on Arc Testnet. You enter a public wallet address. Rebound finds the CCTP burns from that address, matches each one to Circle's signed message, and reads Arc to see whether the mint already happened. Each transfer then gets one plain status: confirming, waiting for Circle, ready to finish, completed, caller required or expired.

For a ready transfer, Rebound prepares the one transaction that finishes it. Circle's signature fixes the recipient, so that transaction can only pay the person the sender originally chose. The same checks are available in the browser inbox, in a local command line tool and, for AI agents, through a read-only MCP server ([agent tools](MCP.md)).

## What problem does it solve?

CCTP moves USDC in three steps. The source chain burns it, Circle signs an attestation, and someone submits that attestation on the destination chain to mint it. The app that started the transfer usually does the last step. If the tab closes, the app crashes or an agent loses its session after the burn, the USDC is gone from Base and not yet on Arc.

The money is not lost, because anyone can still submit the attestation. The trouble is finding it. Existing recovery paths start from the burn transaction hash ([CCTP.io](https://www.cctp.io/docs/recover)) or from the original app's saved bridge result ([Arc App Kit](https://docs.arc.io/app-kit/references/bridge-error-recovery)). Someone who has neither cannot find the transfer. The obvious next move, sending again, costs a second transfer and leaves the first one stuck.

## How does it work?

1. **Discovery.** Rebound scans `DepositForBurn` events from Circle's TokenMessengerV2 on Base Sepolia where the wallet is the depositor or the recipient. It shows the exact block range it searched. A QuickNode Stream index can supply the same events without a scan.
2. **Verification.** Rebound re-fetches the canonical source receipt and pulls the message from Circle's attestation API. It compares amount, recipient, caller restriction, fee, finality and hook against the original burn, and rejects any mismatch or ambiguity.
3. **Destination state.** It reads `usedNonces` on Arc's MessageTransmitterV2 and the message's expiry block. A used nonce means the transfer is completed, whatever Circle's status says.
4. **Finishing.** For a ready transfer, Rebound builds `receiveMessage(message, attestation)`, simulates it from the actual caller, and shows the fixed recipient, protocol fee and gas cap. It broadcasts at most once, after explicit confirmation. It then checks the Arc receipt's `MessageReceived` event against the original nonce and message body.

Provider failures show as unavailable and never as an empty inbox. Rebound holds no funds, takes no fee and never burns again.

**Live proof (November 20, testnet).** We burned 1 USDC on Base Sepolia and deliberately skipped the mint ([source burn](https://sepolia.basescan.org/tx/0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23)). A fresh Rebound session found it from the wallet address alone and marked it ready. Rebound then finished it on Arc ([destination mint](https://explorer.testnet.arc.io/tx/0x92020b4554591c5afc4ae31aff0a8906fc19d1a17f2bd1773e1592762ea0a0a5)), 99 seconds after the burn, and refused a second attempt. [Full record](LIVE-RECOVERY.md)

**Built with:** Circle CCTP V2 and USDC, Arc Testnet, Circle's attestation API, Base Sepolia, QuickNode Streams, Python, FastAPI, Flet and SQLite. No new contracts: Rebound uses Circle's deployed TokenMessengerV2 and MessageTransmitterV2 ([addresses](../README.md#contracts-used)).

## What makes it unique?

- **It starts from an address.** You do not need a transaction hash, a bridge link or the original app.
- **It cross-checks three independent sources.** Rebound calls a transfer ready only when the Base receipt, Circle's attestation and Arc's nonce agree. Completed and ready are decided on chain, not from an API status.
- **It finishes the original transfer instead of retrying it.** The recipient cannot change, a second mint is impossible and nothing is ever burned twice. The tool says so before every signature.
- **Agents can use it.** Payment agents are the clients most likely to lose state mid-transfer. The MCP tools return a `next_step` for each transfer, telling an agent to wait, finish or stop, and never to burn again.

## What would you build next?

- **Mainnet and more routes.** The decoder follows Circle's message format, not a single route, so adding CCTP domains is mostly configuration plus tests against each chain's contracts.
- **Support integrations.** An embeddable status check, so a wallet or exchange support team can answer a "where is my USDC" ticket from a customer's address.
- **Continuous history.** Make the QuickNode Stream index the default discovery path instead of bounded scans.
- **Expired messages.** Request Circle re-attestation from inside the inbox when an attestation has expired.

## Entry details

| Field | Draft |
|---|---|
| Main track | DeFi and Onchain Applications |
| Chain award | Best Use of Arc |
| Repository | [s-k-28/rebound](https://github.com/s-k-28/rebound) |
| Contracts and economics | [Technical README](../README.md#contracts-used) |
| Demo evidence | [Controlled recovery](LIVE-RECOVERY.md) and [independent read-only replay](VALIDATION.md#reproduce-the-read-only-check) |
| Agent tools | [MCP server](MCP.md) |
| Video | Pending verification |
| QuickNode Streams | Add after observing a real Stream delivery |
