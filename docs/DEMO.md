# Demonstrating Rebound

The controlled testnet workflow has verified receipts and a duplicate-preparation refusal ([live recovery evidence](LIVE-RECOVERY.md)). Record the live sequence below when a funded testnet wallet is available. The historical sequence after it uses a second public transfer and read-only simulation, and is the fallback.

## Live recovery video, 2 minutes 40 seconds

**Before recording (about 5 minutes):**

1. Use a throwaway testnet wallet holding Base Sepolia USDC, a little Base Sepolia ETH and Arc Testnet USDC for destination gas.
2. Create the interrupted transfer and note its hash and log index. Do not paste the hash into the inbox you will record.
   ```bash
   uv run python -m rebound.lab interrupt --amount 1 --recipient "$WALLET" --sender "$WALLET"
   ```
3. Wait for Ready after at least 12 Base confirmations and Circle's matching attestation. Check it off screen with a separate database so the recorded inbox starts fresh:
   ```bash
   REBOUND_DB=/tmp/rebound-precheck.sqlite3 uv run python -m rebound.cli inspect "$BURN_TX_HASH"
   ```
4. Start the recorded app and the recovery terminal with the **same** fresh `REBOUND_DB`, so the inbox can show the locally verified receipt before Circle indexes it.
   ```bash
   export REBOUND_DB="$(mktemp -d)/rebound-demo.sqlite3"
   printf 'Use this REBOUND_DB in the recovery terminal: %s\n' "$REBOUND_DB"
   uv run uvicorn rebound.app:app --host 127.0.0.1 --port 8765
   ```
5. Check the inspector's Arc expiry block before the run.

| Time | Screen | Narration |
|---|---|---|
| 0:00–0:20 | The controlled burn on Basescan. Arc has no mint yet. | We paused this 1-USDC testnet transfer after the Base Sepolia burn. Its original mint is still waiting on Arc. |
| 0:20–0:45 | A fresh Rebound tab. Paste only the wallet address and select **Find transfers**. | All we have is the wallet address: no transaction hash and no bridge link. Rebound finds the burn and shows the exact block range it searched. |
| 0:45–1:15 | Select the **Ready to finish** row. Point to each proof step, then to the recipient and the actual amounts shown. | Rebound compares the Base receipt with Circle's signed message and confirms the Arc nonce is unused. The signed message fixes the recipient, and the amounts show Circle's fee and the net mint. |
| 1:15–1:40 | Enter the caller address and select **Review recovery**. | Rebound simulates the finishing call from the signer’s address and displays the current Arc gas estimate. The local command applies the gas cap before signing. |
| 1:40–2:05 | In the terminal, run `uv run python -m rebound.cli recover "$BURN_TX_HASH" "$LOG_INDEX" --caller "$WALLET"` and type `RECOVER`. **Cut the key entry.** End on "Destination mint confirmed". | Signing happens locally. The tool checks everything again, asks for an explicit approval, broadcasts once, and verifies the Arc receipt against the original message. |
| 2:05–2:25 | Back in Rebound, select **Refresh this transfer**, then **Open verified Arc receipt**. | The same transfer is now completed. The Arc receipt's `MessageReceived` nonce matches the original message. |
| 2:25–2:40 | In the terminal, run `uv run python -m rebound.cli prepare "$BURN_TX_HASH" "$LOG_INDEX" --caller "$WALLET"` and show the refusal. | Ask again and Rebound refuses, because Arc has already used this nonce. That means no second mint and no second burn. |

## Historical demo (fallback), 2 minutes 35 seconds

| Time | Screen | Narration |
|---|---|---|
| 0:00–0:20 | Fresh Rebound inbox | A wallet owner can lose the bridge link or transaction hash after USDC is burned. They need to know whether the destination was paid before trying again. |
| 0:20–0:50 | Enter the public sample address and scan the fixed 200-block window | We start with the wallet address. Rebound finds the matching Base Sepolia burn and displays the exact range it checked. |
| 0:50–1:20 | Inspect the 1-USDC transfer | Rebound checks the canonical source receipt, matches Circle’s CCTP message, and reads the nonce on Arc. This sample’s attested fee was 0.00013 USDC, leaving a mint amount of 0.99987 USDC. |
| 1:20–1:55 | Replay the historical mint | The previous Arc block shows an unused nonce. The destination call succeeds in simulation at that block. At the next block, the nonce is consumed and the real receipt matches the original message. This is a replay of an existing public transfer. |
| 1:55–2:20 | Open the Arc receipt and return to the case | The Arc receipt is directly verifiable. Rebound can refresh just this transfer and share a case link pinned to the original burn log. |
| 2:20–2:35 | Show the public repo and scope | Rebound uses the existing CCTP contracts on Base Sepolia and Arc Testnet. For an eligible direct transfer, it can prepare the original mint for local review and signing. |

The recording uses [the public sample](VALIDATION.md#reproduce-the-read-only-check) and its [Arc receipt](https://explorer.testnet.arc.io/tx/0xd2f74e876f5f9b48ca5836044d0b1fa9723de30672587aacb86b55be6d5bdb00).

## Four-minute finalist pitch

| Time | What to show |
|---|---|
| 0:00–0:40 | Open a fresh inbox and explain why the wallet address survives when a bridge session or burn hash is lost. Show the fixed 200-block range as the search begins. |
| 0:40–1:20 | Select the original 1-USDC burn. Point to its canonical Base receipt, exact log index, fixed recipient, and the matching signed Circle message. |
| 1:20–2:25 | Replay the historical mint. Walk through the unused nonce and successful simulation at Arc block 73,127,413. Open the confirmed receipt for the consumed nonce at block 73,127,414. |
| 2:25–3:15 | Read the 0.99987 USDC mint and 0.00013 USDC attested fee from the case. Explain that the caller pays Arc gas separately. Show the exact case link a support operator can recheck. |
| 3:15–4:00 | Show the public repository and release limits. Summarize where the next iteration adds value through broader wallet history and a repeatable controlled testnet recovery. |

If a controlled burn is ready on pitch day, use 1:20–2:25 for the local review, mint and confirmed receipt.

**How does Rebound avoid a second transfer?** It targets the existing CCTP message. Arc’s MessageTransmitterV2 records a consumed nonce after delivery, and Rebound checks it during inspection and again before preparing recovery.

**Who can receive the USDC?** The signed message fixes the mint recipient. The original message can also restrict the destination caller. For app-managed forwarding, the receipt proves the CCTP mint to the encoded recipient and the original app handles downstream actions.

**Why Arc?** The contract the product checks and calls for settlement is on Arc. The review expresses the caller’s gas budget in native testnet USDC separately from the message’s attested fee.

**What does the recorded example prove?** It shows a real receipt and a successful simulation against historical Arc state. The team’s [controlled testnet run](LIVE-RECOVERY.md) separately verifies address-only discovery, local signing and duplicate-preparation refusal.

## Controlled testnet preparation

1. Run the app using the README. Check the configured Base Sepolia and Arc Testnet RPCs, chain IDs, Circle contracts and Circle sandbox availability.
2. Use a dedicated testnet wallet with canonical Base Sepolia USDC and enough Base Sepolia ETH for the source transaction. The caller also needs Arc Testnet USDC for destination gas.
3. Create a small **manual** CCTP V2 transfer from Base Sepolia to Arc Testnet. Specify a recipient you control and an unrestricted zero destinationCaller for the primary demo. Save the source receipt for independent verification, but do not seed the Rebound inbox with its hash.
4. Stop before submitting the destination mint. Do not use an automatically forwarded transfer and pretend that merely closing its UI interrupted Circle's forwarder; forwarding can continue without that page.

## Recording sequence

1. Show the confirmed source burn in an explorer. Explain that the destination mint has not yet happened.
2. Open a fresh app session with no saved transfer history. Enter only the public wallet address and choose a source scan window containing the burn.
3. Show discovery by address, original amount, fixed recipient, route and coverage. Wait for actual attestation availability; do not replace a pending live message with a fixture.
4. Inspect evidence. Show the current nonce check and why this particular transfer is eligible. The attestation may be complete while the mint remains unfinished.
5. Prepare the unsigned transaction from the actual caller. In a separate terminal, run the documented local `recover` command, review the details and explicitly approve the testnet transaction. Hide the key entry in the recording; never reveal even a test key.
6. Open the destination receipt and verify the emitted `MessageReceived` nonce. Return to the app and refresh. The original transfer should now be completed, not available for another mint.
7. Attempt preparation again for the **same source transaction hash and log index**. Capture the completed-state refusal before another transaction can be prepared. This is a state check, not a second burn or another broadcast.
8. Separately show the clearly labeled restricted-caller example and explain why Rebound refuses to bypass the restriction.

## Final sign-off checklist

The first five checks were verified in the [controlled testnet run](LIVE-RECOVERY.md).

- [x] Source receipt proves a small controlled manual burn of canonical Base Sepolia USDC to the intended Arc recipient.
- [x] A fresh app/browser session discovers it using only the public wallet address, with the actual scan range visible.
- [x] Live Circle status, original message fields and unused Arc nonce explain why the mint is eligible.
- [x] The explicitly approved local testnet recovery produces a successful Arc receipt with the expected `MessageReceived` nonce.
- [x] A refreshed inbox shows completed, and a second preparation attempt for the same burn refuses another mint before signing/broadcast.
- [ ] An actual configured QuickNode Stream delivers the known burn to the authenticated HTTPS endpoint. Preserve the Stream ID, deployed filter revision, destination, network/dataset, backfill range and delivery record; redact credentials.
- [ ] Public hosting, desktop/mobile use and the current local-signing handoff are verified. Browser signing is not described as available until a working connector has been tested.
- [ ] The published revision and accessible 2–3 minute video reproduce the evidence above. Example cases remain visibly synthetic.

If no live Stream is configured, mark its gate pending and do not claim the QuickNode bounty integration is complete. If no funded testnet wallet or recovery receipt is available, mark the live recovery pending even when every automated test passes.

## Evidence to preserve

- Source burn transaction hash, log index and block hash.
- Supported native USDC amount, destination domain and original recipient.
- Circle message nonce and attestation status at inspection time.
- Destination transaction hash and receipt for the matching nonce.
- Exact source history range and configured index feed.
- Whether any displayed case is a synthetic fixture.

Use small testnet values. Do not claim recovery of mainnet funds, universal bridge support, validated user demand or deployed QuickNode infrastructure from this procedure alone.

## If something fails

- **No matches:** check the source block range, native token and supported event/route. A scan is not a search of every chain or all history.
- **Attestation pending or 404:** keep the original burn and refresh; do not send it again.
- **Expired:** follow Circle's re-attestation procedure for the existing burn.
- **Caller restricted:** use the actual authorized caller or an independently reviewed contract adapter. A different signing key cannot override the signed message.
- **Broadcast outcome unknown:** inspect the locally printed destination hash before another attempt; the CLI never automatically resends.
- **Provider unavailable:** show the error honestly. A polished example screen is not proof of a live recovery.
