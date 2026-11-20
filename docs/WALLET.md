# Finish in a browser wallet

A live transfer in the `ready` state can be finished from an injected browser wallet (MetaMask, Rabby or Coinbase Wallet extension). It is the browser alternative to `python -m rebound.cli recover`. **Arc Testnet only.** The server prepares and revalidates, the Flet UI shows the review and takes the approval, and the wallet signs and broadcasts. No seed phrase or private key is requested or sent to the server.

**Off by default.** Set `REBOUND_BROWSER_WALLET=1` to enable it. It stays off until the real-extension run below passes on a funded throwaway Arc Testnet account. When it is off, the wallet routes are not mounted and the inbox is unchanged.

When it is on, **Finish with browser wallet** is the one primary button in **Finish the transfer** for live `ready` transfers, and **Review recovery** (the CLI path) becomes secondary. It never appears for example transfers or any other status.

## Design

```
Flet UI (rebound/wallet_ui.py)          server (rebound/wallet_session.py)        wallet tab (rebound/wallet.py + wallet_adapter.js)
------------------------------          ----------------------------------        ------------------------------------------------
Finish with browser wallet  ──open────▶ session: created (or the transfer's held session)
Connect wallet (opens tab) ─────────────────────────────────────────────────────▶ /wallet/adapter#<one-time code>
                                        exchange ◀───── POST /exchange {code} ─── token kept in JS memory
                                                                                  eth_requestAccounts, switch/add Arc
                                        connected ◀──── POST /connected ───────── {account, chainId}
                                        prepare(tx, log, caller=account)
review shown in Flet ◀──── poll ─────── reviewed: exact tx + fee caps stored
Send from wallet ─approve(generation,tx)▶ same review? fresh prepare, reservation taken
                                        approved ◀──── poll ────────────────────── sees "approved"
                                        sending  ──── POST /claim ──────────────▶ stored tx, checked field by field
                                                                                  eth_sendTransaction (once)
                                        submitted ◀── POST /result {hash} ─────── or rejected / not_sent / unknown
verify ───────────────────────────────▶ canonical receipt check
verified receipt shown in Flet ◀─────── confirmed | failed | unrelated
```

Injected EIP-1193 wallets are reachable only from page JavaScript, and Flet has no wallet bridge. The wallet tab is therefore the app's only HTML and JavaScript. It has one status line and no review UI: everything the user reads or approves is rendered by Flet.

### Session API (used only by the wallet tab)

Sessions are created in-process by the Flet UI. There is no public create endpoint. A session id is 128 random bits and acts as the tab's capability, so **it never appears in a URL** and never reaches an access log:

- The Flet page holds the session object in Python. The **Connect wallet** link is `/wallet/adapter#<code>`, where the code is a separate 128-bit one-time value. Browsers never send the fragment to a server.
- The script removes the fragment from the address bar and history, then trades the code in a POST body for the session id. A used code is dead, and a fresh one replaces it for **Open wallet tab again**.
- The script keeps the id in memory and uses `Authorization: Bearer <token>` on same-origin requests. Access logs and tracing should redact this header.

| Route | Purpose |
|---|---|
| `GET /wallet/adapter` | The wallet tab. The same page for every session. |
| `POST /api/wallet-session/exchange` | `{code}` → `{token, state, …}`. 404 for an unknown or used code. |
| `GET /api/wallet-session` | `{state, account, chain_id}`. Never includes the transaction. It is also the tab's heartbeat. |
| `POST /api/wallet-session/connected` | `{account, chainId}`. Chain must be `0x4cef52`. Runs the review. |
| `POST /api/wallet-session/claim` | `approved → sending`. Returns the stored transaction exactly once per approval. |
| `POST /api/wallet-session/result` | `{outcome: submitted, hash}`, `rejected` (4001), `not_sent` (the tab's own check failed) or `unknown`. |

Every transition is validated in `WalletSessions`. A request in the wrong state gets 409.

## Review and fee caps

On connect, the server runs `RecoveryService.prepare(tx, log, caller=<connected account>)`. That rechecks the source burn, the attestation and the Arc nonce, simulates `receiveMessage` from that account and estimates gas. The result goes through `cli.validate_prepared`. The server then stores the exact wallet transaction:

`{from, to: MessageTransmitterV2, data, value: 0x0, gas, maxFeePerGas, maxPriorityFeePerGas, chainId: 0x4cef52}`

- `maxPriorityFeePerGas` is Arc's `eth_maxPriorityFeePerGas`. `maxFeePerGas` is `2 × latest baseFeePerGas + maxPriorityFeePerGas`.
- The review shows the gas limit, the max fee per gas and **maximum network fee = gas × maxFeePerGas**, computed from the same stored fields that are sent. The caller's balance is compared with that maximum. A shortfall blocks sending and links [faucet.circle.com](https://faucet.circle.com).
- A maximum above 0.1 testnet USDC (the CLI's default `--max-gas-usdc`) is refused.
- Flet states: "Your wallet may show a lower final fee; it cannot exceed this maximum unless you edit it in the wallet." Wallets let users edit fees, and Rebound cannot prevent that.

**Send from wallet** carries the generation and exact transaction captured by the rendered button. The server checks both before any asynchronous work and again after checking the receipt of an earlier attempt. It then reserves the transfer and runs `prepare` again from the same account. The calldata must be unchanged, the gas estimate must not have risen and the balance must still cover the maximum.

The wallet tab sends only what it claimed, and only if all of the following hold. Otherwise it reports `not_sent`:

- The field set is exactly the eight fields above.
- `to` is the transmitter, `from` is the connected account, `chainId` is Arc, `value` is `0x0` and the calldata has the `receiveMessage` selector.
- The fee caps are well-formed with priority ≤ max, and gas × maxFeePerGas is within the cap.
- The wallet is still on Arc with the same account.

## Duplicate sends

- **Per approval.** The server hands out the stored transaction once (`claim`). The tab calls `eth_sendTransaction` at most once per approval. A wallet rejection (`4001`) means nothing was sent: the session returns to review and the user may approve again.
- **Per transfer, across tabs and browsers.** The reservation is server-side and keyed by `tx_hash:log_index`. While any session for that transfer is `approving`, `approved`, `sending`, `submitted`, `unknown` or `unrelated`, no other session can be approved. It is released only when:
  - the hash is verified (`confirmed`), or its receipt is provably this attempt's revert (`failed`; see below)
  - the tab reports a rejection or that it did not send
  - an approval is not claimed within 90 seconds (nothing reached the wallet)
  - the user, after checking wallet activity, chooses **My wallet sent nothing**. This is offered only in `unknown` or `unrelated`, never while `approving` or `sending`, when the wallet could still submit the claimed transaction.
- **Resuming.** The Flet tab keeps its own session when the dialog closes while a transfer is reserved. Reopening the dialog in that tab resumes the attempt. Another tab or browser sees its own wallet session and cannot clear the first tab's reservation. The hash field remains available while a send may have happened (`sending`, `submitted`, `unknown`, `unrelated`).
- **A silent tab.** While the wallet shows its confirmation, the tab keeps polling. After five minutes without contact, `sending` becomes `unknown`. Rebound then asks the user to reject any pending request in the wallet and check its activity before releasing.
- **Scope.** Reservations live in the memory of one Rebound server process. They cover every tab and browser served by that process. They do not survive a restart, are not shared between processes or workers, and do not cover the CLI or other wallets sending the same message. A competing send can still cost gas, but it cannot mint twice: Arc's nonce check makes the second one revert.
- **Unknown outcome.** Any wallet error other than `4001`, or a missing hash, becomes `unknown`. Rebound never retries. The user can paste the hash from wallet activity to verify it, or release the transfer after confirming nothing was sent.

## Hash verification

`POST /result` only records the hash. The wallet flow adds no verifier of its own. `WalletSessions.verify` first binds the hash to the session with one receipt read:

- no receipt means pending
- the receipt must have the same transaction hash
- `to` must be MessageTransmitterV2 and `from` must be the connected account
- status `0x0` counts as this attempt's revert only if the claiming wallet adapter reported the hash and all of these checks hold. A pasted hash can prove successful delivery but cannot release a failed send.
  - the RPC reports Arc Testnet, and the canonical block at the receipt's height includes the hash at the receipt's transaction index
  - the block is newer than the Arc head read at approval, so the receipt cannot belong to an earlier attempt
  - `eth_getTransactionByHash` agrees on the block and index and shows the same hash, approved sender, transmitter, zero value and exact approved calldata

Delivery proof is main's exact-hash check, `RecoveryService.inspect_transaction(tx_hash, log_index=…, destination_hash=…)`. That call re-derives the original burn and Circle attestation and requires the Arc nonce to be consumed. It then runs `verify_arc_mint_receipt` on that exact hash:

- the block at the receipt's height has the receipt's block hash, and the transaction sits at its index in that block
- a `MessageReceived` log from the transmitter carries this transfer's nonce, source domain, sender, finality and exact message body

The hash is shown as the delivery receipt only when the returned row for this `tx_hash:log_index` has `submitted_receipt_verified: true`.

| Result | Session state | Reservation |
|---|---|---|
| No receipt yet | `submitted`; Flet keeps checking while the dialog is open (up to 15 minutes), then offers **Check receipt again** | held |
| All checks pass | `confirmed`; Flet shows **Open verified Arc receipt** | released; the receipt is remembered for the inbox |
| Reverted, and provably this attempt | `failed`: "did not deliver this transfer; gas may have been spent" | released |
| Reverted, but not canonical, older than the approval, or with other calldata or sender | `unrelated` | held |
| Successful but unrelated (wrong nonce or body, wrong contract, wrong sender, different hash, non-canonical block, nonce not consumed) | `unrelated`: "This hash does not prove delivery of this transfer" | held, because the real send may still be pending |

## Wallet tab security

`/wallet/adapter` serves one status line and one inline script (`rebound/wallet_adapter.js`, with constants injected from `rebound/protocol.py`). It has no styles, forms, inputs or images, and loads nothing from other origins. Server data is written with `textContent` only.

Headers:

- `Content-Security-Policy: default-src 'none'; script-src 'nonce-…'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`
- `X-Frame-Options: DENY`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`

Wallets that lack Arc are offered only the public RPC, `https://rpc.testnet.arc.network`. The server's `ARC_TESTNET_RPC`, which may carry credentials, is never sent to the browser.

## Test with a real extension (pending)

Use a **throwaway** Testnet account funded only from the faucet. Never import a key that holds real assets.

1. Run `REBOUND_BROWSER_WALLET=1 uv run uvicorn rebound.app:app --port 8765` and open the inbox. Find a live transfer that shows **Ready to finish**, for example one created with the [controlled transfer lab](LAB.md).
2. Click **Finish with browser wallet**, then **Connect wallet**. Approve the connection and the Arc Testnet network in the wallet. Return to the Rebound tab.
3. Check the review: recipient, amounts, gas limit, max fee per gas and the maximum network fee. In the wallet's confirmation, the site-suggested max fee should equal the reviewed one.
4. Click **Send from wallet** and confirm in the wallet. Rebound should show the submitted hash, then the verified receipt. Close the dialog: the transfer refreshes to **Completed**.
5. Optional: open the dialog in a second browser before sending. Its **Send from wallet** should be refused while the first send is pending.
6. Optional: close the dialog while the wallet confirmation is open, then reopen the transfer. It should show the same attempt, not a new one.
