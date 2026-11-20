# Finish in a browser wallet

`/finish/{burn_tx_hash}/{log_index}` lets the user finish one ready transfer with an injected browser wallet (MetaMask, Rabby or Coinbase Wallet extension, or a mobile wallet's in-app browser). It is the browser alternative to `python -m rebound.cli recover`. **Arc Testnet only.** The server prepares and revalidates. The wallet signs and broadcasts. No seed phrase or private key is requested by the page or sent to the server.

The inbox shows **Finish in browser wallet** for live transfers in the `ready` state. It never appears for example transfers.

## Flow

1. **Load.** The page reads `GET /api/transactions/{hash}?log_index=` and shows the amount, the fixed recipient and the current state. If the state is not `ready`, the page explains it and does not offer signing.
2. **Connect.** `eth_requestAccounts`, then `wallet_switchEthereumChain` to `0x4cef52` (5042002). If the wallet does not know Arc, the page first checks that `https://rpc.testnet.arc.network` reports `eth_chainId = 0x4cef52`, then calls `wallet_addEthereumChain` (native currency USDC, 18 decimals, explorer `https://explorer.testnet.arc.io`).
3. **Review.** `POST /api/prepare` with the connected account as caller. The page shows the recipient, original amount, protocol fee, net amount, maximum Arc gas, the caller's Arc balance and any shortfall. A shortfall blocks the send and links the [Circle faucet](https://faucet.circle.com).
4. **Finish.** The page calls `/api/prepare` again immediately before sending and runs the checks below. It then sends one `eth_sendTransaction` with `{from, to, data, value: "0x0", gas, chainId}`.
5. **Track.** The page polls the receipt through the wallet. If the wallet has left Arc, it polls the public Arc RPC. After a successful receipt, it re-reads the transfer until Rebound reports `completed`, then links the Arc transaction and the Base Sepolia source.

## Checks before the wallet opens

The second preparation must pass every check, or nothing is sent:

- `to` is Arc's MessageTransmitterV2 (`0xe737…e275`, injected from `rebound/protocol.py`), `value` is `0x0`, `chainId` is 5042002, and `from` is the connected account.
- The wallet's own `eth_chainId` is `0x4cef52`. `eth_accounts[0]` is still the reviewed account.
- The page decodes the `receiveMessage(bytes,bytes)` calldata independently. The source domain must be 6 and the destination domain 26. The message must carry no hook, and its destination caller must be zero or the connected account. The encoded recipient, nonce, amount and fee must match both the server's summary and the transfer state.
- The recipient, amount, net amount, fee, nonce and calldata must equal the reviewed values. Gas must be within the service bound. The gas estimate must not have risen, and there must be no shortfall.

## Duplicate sends and failures

- **Duplicate sends.** The Finish button is disabled while a request is open. The sent hash is stored in `sessionStorage` under `rebound:finish:{hash}:{log}`, so after a refresh the page tracks that hash instead of offering another send.
- **Rejection.** If the user rejects in the wallet (`4001`), the record is cleared. Nothing was sent.
- **Unknown outcome.** Any other wallet error after the request opens is treated as unknown. The page asks the user to check wallet activity and does not resend automatically. A reverted receipt is reported and is not retried.
- **Wallet changes.** A changed account or network resets the review.

## Page security

The page is the app's only authored HTML/JavaScript, isolated in `rebound/wallet.py` and `rebound/wallet.html`. It loads no external scripts, styles or fonts. Manrope is served from `/finish/manrope.ttf`. Server data is written with `textContent` only.

Response headers:

- `Content-Security-Policy`: `default-src 'none'`, with a per-response nonce for the one inline script and stylesheet. `connect-src` allows only `'self'` and the public Arc RPC, plus `frame-ancestors 'none'`, `base-uri 'none'` and `form-action 'none'`.
- `X-Frame-Options: DENY`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`.

The server's configured `ARC_TESTNET_RPC`, which may carry credentials, is never sent to the browser.

## Test with MetaMask on Arc Testnet

1. Run `uv run uvicorn rebound.app:app --port 8765` and open the inbox. Find a live transfer that shows **Ready to finish**.
2. Use a throwaway MetaMask account. Add Arc Testnet USDC for gas from [faucet.circle.com](https://faucet.circle.com).
3. Click **Finish in browser wallet**, or open `http://127.0.0.1:8765/finish/<burn hash>/<log index>` directly.
4. Click **Connect wallet**. Approve the connection and the Arc Testnet network request.
5. Check the review. It should show the fixed recipient, the net amount and the gas. Click **Finish transfer** and confirm in MetaMask.
6. The page should show the Arc transaction and then **Transfer finished**. Refresh it: the page should keep tracking the same hash and offer no second send. Reopen the transfer in the inbox: it should show **Completed**.

To check the not-ready path, open `/finish/0xcfe897ab891b667c9ae538d9d6713c02db167dfd0a058fc6c09313734de0ecbb/4`. That transfer is already completed, so the page shows the verified Arc receipt and no signing controls.
