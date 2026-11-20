# Demonstrating Rebound

Use the controlled testnet recording when the complete workflow is verified. The historical recording below uses a verified public transfer and read-only simulation.

## Historical demo, 2 minutes 35 seconds

| Time | Screen | Narration |
|---|---|---|
| 0:00–0:20 | Fresh Rebound inbox | A wallet owner can lose the bridge link or transaction hash after USDC is burned. They need to know whether the destination was paid before trying again. |
| 0:20–0:50 | Enter the public sample address and scan the fixed 200-block window | We start with the wallet address. Rebound finds the matching Base Sepolia burn and displays the exact range it checked. |
| 0:50–1:20 | Inspect the 1-USDC transfer | Rebound checks the canonical source receipt, matches Circle’s CCTP message, and reads the nonce on Arc. This sample’s attested fee was 0.00013 USDC, leaving a mint amount of 0.99987 USDC. |
| 1:20–1:55 | Replay the historical mint | The previous Arc block shows an unused nonce. The destination call succeeds in simulation at that block. At the next block, the nonce is consumed and the real receipt matches the original message. This is a replay of an existing public transfer. |
| 1:55–2:20 | Open the Arc receipt and return to the case | The Arc receipt is directly verifiable. Rebound can refresh just this transfer and share a case link pinned to the original burn log. |
| 2:20–2:35 | Show the public repo and scope | Rebound uses the existing CCTP contracts on Base Sepolia and Arc Testnet. For an eligible direct transfer, it can prepare the original mint for local review and signing. |

The recording uses [the public sample](VALIDATION.md#reproduce-the-read-only-check) and its [Arc receipt](https://explorer.testnet.arc.io/tx/0xd2f74e876f5f9b48ca5836044d0b1fa9723de30672587aacb86b55be6d5bdb00).

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

All items are pending until independently recorded; this document does not certify a completed live run.

- [ ] Source receipt proves a small controlled manual burn of canonical Base Sepolia USDC to the intended Arc recipient.
- [ ] A fresh app/browser session discovers it using only the public wallet address, with the actual scan range visible.
- [ ] Live Circle status, original message fields and unused Arc nonce explain why the mint is eligible.
- [ ] The explicitly approved local testnet recovery produces a successful Arc receipt with the expected `MessageReceived` nonce.
- [ ] A refreshed inbox shows completed, and a second preparation attempt for the same burn refuses another mint before signing/broadcast.
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
