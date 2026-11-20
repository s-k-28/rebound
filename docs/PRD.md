# Rebound — Product Requirements Document

Version 0.1 · November 19, 2026 · Owner: s-k-28

## 1. Product decision

Rebound helps a person find and finish an interrupted USDC transfer after losing the original application's saved state. A public wallet address is the starting point. The product reconstructs supported transfers from chain events, distinguishes waiting from actionable failures, and prepares completion of the original transfer to its immutable recipient.

The user authorized writing this PRD and implementing it. The selected implementation is **Python 3.13+, Flet/Flutter UI, FastAPI, HTTPX, eth-abi, eth-account, SQLite**. No application HTML, CSS or TypeScript will be authored. The frontend framework itself supplies browser assets. Rust and Next.js proposals were rejected by the user.

## 2. Problem and evidence

A confirmed source burn does not mean USDC arrived at the destination. Circle separates burn, attestation and mint. Existing recovery methods often start from a transaction hash or saved application state. CCTP.io explicitly documents hash-based lookup and browser-local history, and says it does not search all transfers by a wallet. Arc App Kit retries an existing BridgeResult.

This supports an address-first recovery workflow; it does not prove market size, willingness to pay, or global novelty. No customer interviews or recovered-funds metrics have been collected. Range already provides broad CCTP visibility; Fivo already automates reconciliation of known merchant payments. Rebound must outperform a generic retry button through discovery, accurate diagnosis and understandable next actions.

Sources:

- https://www.cctp.io/docs/recover
- https://docs.arc.io/app-kit/references/bridge-error-recovery
- https://developers.circle.com/cctp/howtos/troubleshoot-transfers
- https://developers.circle.com/cctp/howtos/retry-failed-mint
- https://developers.circle.com/cctp/references/contract-addresses
- https://github.com/circlefin/evm-cctp-contracts/tree/master/src/messages/v2

## 3. User and job

Primary user: someone who sent native USDC over a supported CCTP route and cannot tell whether it finished. Secondary user: a wallet support operator investigating a customer's public address.

Job: “When my transfer appears unfinished and I no longer have its original screen, help me find its real state and take the correct next step without sending the money again.”

Recovery is episodic. Daily consumer retention is not established; a support integration is a future hypothesis rather than claimed adoption.

## 4. Success and release gates

The first build is successful when it can:

1. Find an eligible burn from an address within an explicitly displayed source block range, without a saved hash.
2. Decode its original amount, depositor, recipient, destination and caller restrictions.
3. Reconcile canonical source receipt, Circle message and destination nonce state.
4. Distinguish ready, waiting, completed, restricted, expired, unsupported and unavailable states.
5. Revalidate and simulate the exact destination transaction before preparing it.
6. Offer a local Python signing command that revalidates again and requires explicit human confirmation before a **testnet-only** broadcast.
7. Keep examples visibly separate from live results and disallow signing examples.

Automated gate: protocol decoding and state tests pass, including mismatched recipient/amount, malformed messages, duplicate/reordered messages, restricted callers, expired attestations, already-used nonces, RPC errors and incomplete history. API validation and UI construction checks pass. A live read-only smoke check verifies the configured networks/contracts when providers are reachable.

Hackathon-ready gate remains separate: a real controlled testnet burn, discovery in a fresh session, successful mint and destination receipt, deployed accessible app, real configured Stream if entering that bounty, public source, README and 2–3 minute video. Passing unit tests alone does not meet this gate.

## 5. Scope and priorities

### P0 — implement now

| ID | Requirement | Acceptance criterion |
|---|---|---|
| R1 | Address-first scan | Valid EVM address; bounded Base Sepolia event scan; match depositor or decoded recipient; show exact coverage and limitations |
| R2 | Accurate transfer identity | Use source transaction + log index + canonical block identity; separate same-value transfers; decode native USDC burns only |
| R3 | Durable discovery | Persist event candidates in SQLite; cached or webhook data is discovery evidence only, never authority to prepare a transaction |
| R4 | Diagnosis | Re-fetch successful canonical source receipt, match a Circle message to the particular burn, query Arc nonce and block; network failures never become “ready” |
| R5 | Clear next action | Waiting means refresh; completed means no retry; restricted means caller required; expired means re-attestation needed; unsupported means outside current coverage |
| R6 | Recovery preparation | Revalidate source/message/nonce, supplied caller authorization, fee bounds, version, route, recipient and source asset; simulate receiveMessage; export an unsigned transaction |
| R7 | Local execution | CLI takes a fresh preparation request, prompts for a testnet key locally, matches its address, shows recipient/net amount/gas estimate, asks for confirmation, broadcasts once and checks the receipt |
| R8 | Example mode | Clearly labeled synthetic cases for ready/waiting/restricted/completed; no onchain-action controls in example mode |
| R9 | User-facing app | Responsive Python Flet interface with scan, filters, empty/loading/error states, transfer details, evidence and transaction-review view |
| R10 | Reproducibility | Pinned lockfile, .env.example, setup/run/test instructions, contract/source references, architecture and limitations |

### P1 — next milestone, not silently included

- Browser/mobile wallet connector compatible with Flutter/Flet; no seed phrase in the web UI.
- Authenticated QuickNode Stream deployment and historical backfill. Implement an adapter/filter and instructions now; bounty qualification requires an actual configured service and observed delivery.
- User-triggered re-attestation with fresh status polling; the initial build diagnoses expiry and links the canonical procedure.
- Public hosting and a complete live recovery video.

### Out of scope

Mainnet; arbitrary chains/assets/bridges; reversing transfers; changing recipients; recovering private keys; bypassing destinationCaller; insuring losses; depositing/burning again as recovery; speculative liquidity or premium markets; LLM-controlled financial decisions; fabricated adoption metrics.

## 6. Route and history contract

Only **Base Sepolia (chain 84532, CCTP domain 6) → Arc Testnet (chain 5042002, domain 26)** is supported initially. Native source USDC is `0x036CbD53842c5426634e7929541eC2318f3dCF7e`. TokenMessengerV2 is `0x8FE6B999Dc680CcFDD5Bf7EB0974218be2542DAA`; MessageTransmitterV2 is `0xE737e5cEBEEBa77EFE34D4aa090756590b1CE275`. Verify chain IDs and deployed code before use.

Discovery covers canonical TokenMessengerV2 DepositForBurn events in the requested range. It matches the depositor or mintRecipient. It cannot infer arbitrary ownership through unknown routers. An empty result means no matches **within that coverage**, never proof of no unfinished transfers anywhere. Scan defaults to 2,000 source blocks and caps at 50,000; provider restrictions and partial failures are visible. Historical Stream candidates can extend discovery but are individually revalidated.

## 7. State machine

`discovered → source_confirming → awaiting_attestation → ready → submitted → completed`

Branches: `caller_required`, `expired`, `unsupported`, `unavailable`, `source_changed`.

- A pending or missing Circle attestation is waiting, not permanent loss.
- Circle status “complete” describes attestation availability, not successful mint.
- Used destination nonce is completed even if the old attestation has since expired.
- Nonzero destinationCaller may be an EOA or contract. Only that caller can deliver the message. The initial implementation does not invent a contract adapter.
- Zero expiration block means no expiry. An expiration boundary is checked against destination chain rules.
- Never collapse a provider outage or malformed API response into an empty successful result.

## 8. Interaction and visual design

The initial frontend was rejected as generic. The revised interface is a developed finance workspace: persistent navigation, compact transfer rows, derived status counts and a selected-transfer inspector. Forest ink `#173B32`, action `#28624F`, mint `#C9E8D8`, paper `#F4F6F3`, charcoal text `#172823`, and restrained amber waiting states. Use bundled Manrope typography and a state-driven route connecting source and destination. Avoid fictional balances or “funds recovered” totals. [Design sources](DESIGN-SOURCES.md) records the niche template studios and the original implementation decisions.

```
Rebound                         Base Sepolia → Arc Testnet    Testnet

Transfer inbox
Find what's unfinished.
[ Public wallet address                         ] [Find transfers]
Recent history: [2,000 source blocks]

[All] [Needs attention] [Completed]             [Explore examples]
USDC amount   Source → destination   State          [Inspect]

Detail: amount / fixed recipient / current state / source receipt
        burn → attestation → destination mint
        caller rule / nonce verification / coverage / next action
```

On small screens rows stack; addresses are selectable; controls keep visible labels; errors explain next steps. Keyboard interaction and readable contrast matter more than ornamental animation. Example mode has a persistent banner and returns explicitly to live mode.

## 9. Architecture and language rationale

Python handles async RPC/API I/O, pure message decoding/state functions, SQLite candidate storage, FastAPI routes and Flet controls. Flet renders Flutter on web; application code remains Python rather than authored HTML/CSS/TypeScript. This workload is network-bound, so Rust's raw throughput is not a present requirement. Python provides easier protocol tests and one language for collaborators. Flet's wallet ecosystem is weaker than a conventional web stack; local CLI signing is a transparent first-build compromise, not an assertion of equivalent wallet UX.

Modules: `protocol.py` (decoding and invariants), `clients.py` (RPC/Circle), `service.py` (discovery/reconciliation/preparation), `store.py` (candidate index), `ui.py` (Flet), `app.py` (ASGI/API), `cli.py` (local signing), `examples.py` (synthetic presentation fixtures only). UI does not implement a second financial state machine.

## 10. Transaction integrity and operations

No key is accepted by web endpoints. CLI keys are read by a terminal prompt, never logged or persisted. Re-fetch and simulate from the actual signer immediately before broadcast. `receiveMessage(bytes,bytes)` target and zero value are fixed by the service; destination is testnet-only. Protocol nonce protection prevents a second successful mint, but racing attempts may still cost gas. Display this limitation. A successful simulation is not a guarantee of later execution.

Source RPC, destination RPC and Circle sandbox endpoints are operator configuration, not request-provided URLs. Validate chain IDs, response structure, address padding, message versions, fees and domains. Bound scans/concurrency and request sizes. Do not expose raw provider error strings containing credentials. Stream ingestion requires a configured secret and payload limit, stores candidates only and must tolerate duplicates/reorgs. Public deployment needs further abuse/load review and HTTPS.

## 11. Delivery order

1. Write PRD and protocol tests; implement decoder/invariants.
2. Add real RPC/Circle clients, bounded discovery, SQLite, diagnosis and preparation tests.
3. Build Flet inbox, detail and review states; add truthful examples.
4. Add local testnet signing CLI, Stream adapter, API and setup docs.
5. Run full tests/lint and read-only network smoke checks; visually inspect app in dedicated Chrome.
6. Push reviewed code and PRD to GitHub; invite the three user-provided collaborators. Keep personal email addresses outside the public repository.

## 12. Event constraints and open risks

UBC submissions close November 20, 2026 at noon CST. The planned entry is DeFi and Onchain Applications with Best Use of Arc. The official guide lists CCTP cross-chain flows under DeFi and requires a README explaining economic design, risks and contract addresses. The QuickNode bounty requires a configured Stream with a filter and destination. Devpost lists an age-of-majority requirement and the event requires in-person attendance on November 21 to qualify for prizes. [Hackathon guide](https://docs.google.com/document/d/1QaySCo7u3VxMVNdk3C5Ab3ewc2E46AuLntti-Uzx5EY/edit) · [Devpost](https://ubc-hackathon-2026.devpost.com/)

Largest product risks: competing recovery tooling, episodic use, incomplete historical coverage and unvalidated demand. Largest implementation risks: provider history limits, attestation outages, wrong transfer/message association, restricted callers and browser signing compatibility. No guaranteed-win or universal-recovery claim is permitted.

### Overnight product decisions

The research pass issued 500 search queries across event rules, adjacent products, CCTP, Arc, QuickNode, defensive controls, deployment, templates, motion, and transaction UX. The Devpost gallery was unpublished during review. Public entrant counts do not reveal the number of teams pursuing transfer recovery. [Event listing](https://ubc-hackathon-2026.devpost.com/) · [Gallery](https://ubc-hackathon-2026.devpost.com/project-gallery)

Public projects show adjacent overlap. ARTIC verifies receipts to resume marketplace actions, and Clause uses signed QuickNode deliveries to track its escrow. Circle’s App Kit can resume an existing bridge result. Rebound’s testable distinction is recovery evidence starting from a wallet address whose original bridge context is unavailable. [ARTIC](https://devpost.com/software/artic-6a27xr) · [Clause](https://devpost.com/software/clause-gy3dur) · [App Kit recovery](https://community.arc.io/public/blogs/app-kit-faqs-2026-07-07)

Focus the release on Base Sepolia → Arc Testnet, exact receipt verification, bounded public requests, and a responsive evidence inspector. Keep browser signing, more chains, and an automated relayer for later validation. The fake-wallet workflow uses synthetic data. Bounty proof requires an actual Stream-delivered burn that appears in the live index.
