# Devpost gallery

Upload these in order. Each caption is ready to paste into Devpost. All images are 1500 × 1000 JPG.

| # | File | Caption | Data |
|---|---|---|---|
| 1 | [01-ledger-inspector-completed.jpg](01-ledger-inspector-completed.jpg) | Check a live sample finds the controlled 1 USDC transfer, burned at 06:43:33 UTC and minted on Arc 99 s later. | Live: Base [`0x7e43…3f23`](https://sepolia.basescan.org/tx/0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23) log 4, Arc [`0x9202…a0a5`](https://explorer.testnet.arc.io/tx/0x92020b4554591c5afc4ae31aff0a8906fc19d1a17f2bd1773e1592762ea0a0a5) |
| 2 | [02-ready-to-finish.jpg](02-ready-to-finish.jpg) | A case link opens a real 1 USDC burn that is ready to finish, with its proof, fixed recipient and amounts. | Live: Base [`0x9064…50b5`](https://sepolia.basescan.org/tx/0x9064f18850c6a86cdea854175835f8eabb3a20eb06a2f1598f6fa0675cc950b5) log 5, Arc nonce unused |
| 3 | [03-review-recovery.jpg](03-review-recovery.jpg) | Review recovery rechecks the transfer, simulates the Arc mint and shows the unsigned transaction and gas budget without signing it. | Live: same burn, caller `0x3958…722c`; nothing was signed or sent |
| 4 | [04-history-utc-cutoff.jpg](04-history-utc-cutoff.jpg) | A wallet lookup ending at 06:44 UTC scans 200 Base Sepolia blocks and finds the controlled transfer. | Live: blocks 61,175,661–61,175,860 |
| 5 | [05-phone-inspector.jpg](05-phone-inspector.jpg) | At 390 px, ledger rows stack and the inspector opens as a full-width sheet, shown on the real ready transfer. | Live: same burn as image 2 |
| 6 | [06-examples-synthetic.jpg](06-examples-synthetic.jpg) | Example mode shows Ready to finish, Waiting for Circle, Caller required and Completed with synthetic transfers that cannot be signed. | Synthetic: `rebound/examples.py` |
| 7 | [07-recover-terminal.jpg](07-recover-terminal.jpg) | Recorded output of the recover command from the November 20 controlled run, up to the confirmed Arc mint. | Live run in [LIVE-RECOVERY.md](../../LIVE-RECOVERY.md#3-destination-mint) |

## How they were made

- **Images 1–6** are captures of the app from `main` at `5d1cd9d`, run locally with an empty `REBOUND_DB` and the default public Base Sepolia, Circle sandbox and Arc Testnet endpoints. They were taken in Chrome on November 20, 2026, between 08:26 and 08:34 UTC. The relative times ("1 h 43 min ago", "Unfinished for 1 h 27 min") are from that moment. The later commit `cd9af07` only adds accessibility labels and an end-block validation message, which do not appear in these views.
- **Desktop images** use a 1200 × 800 px window at 2x. Each was captured in four quadrants, joined and scaled to 1500 × 1000 with no other edits.
- **Image 5** places three 390 × 844 px screens from the same session side by side on a plain background. It has no device frame.
- **Image 3** used **Review recovery** only. That step rechecks the burn, simulates `receiveMessage` from the caller and shows unsigned JSON. Burn `0x9064…50b5` log 5 was not signed, sent or recovered.
- **Image 6** is the only synthetic image. The app labels it with the "Example mode" notice and "SIMULATED PROOF · Example wallet".

### Image 7 text

Image 7 is typeset from the recorded terminal log of the run in [LIVE-RECOVERY.md](../../LIVE-RECOVERY.md). It is not a screenshot. The log was recorded through an `expect` session, so its first line starts with `spawn`. The image shows that as `$`, wraps long lines at the image width and stops at the confirmation line, before the JSON receipt. The private key prompt was hidden, so nothing follows it.

```text
$ uv run --locked python -m rebound.cli recover 0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23 4 --caller 0x395809d8da4a70145d5060448070a15367df722c
Testnet recovery: Base Sepolia → Arc Testnet (chain 5042002)
Source burn: 0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23, log 4
Fixed recipient: 0x395809d8da4a70145d5060448070a15367df722c
Original amount: 1 USDC
Recipient receives: 0.99987 USDC; protocol fee: 0.00013 USDC
Estimated maximum gas cost: 0.005351325 testnet USDC
Local fee cap: 0.1 testnet USDC
No new burn is created. Racing attempts may still cost gas. Simulation does not guarantee execution.
Type RECOVER to approve this testnet transaction: RECOVER
Throwaway Arc Testnet private key (hidden; never saved):
Signed transaction hash: 0x92020b4554591c5afc4ae31aff0a8906fc19d1a17f2bd1773e1592762ea0a0a5
Submitted once. Waiting up to 60 seconds for a destination receipt.
Destination mint confirmed: https://explorer.testnet.arc.io/tx/0x92020b4554591c5afc4ae31aff0a8906fc19d1a17f2bd1773e1592762ea0a0a5
```

## Reproduce

```sh
REBOUND_DB=data/gallery.sqlite3 uv run uvicorn rebound.app:app --host 127.0.0.1 --port 8765
```

- **Image 1:** open `/` and select **Check a live sample**, then the row.
- **Images 2 and 3:** open `/transfer/0x9064f18850c6a86cdea854175835f8eabb3a20eb06a2f1598f6fa0675cc950b5/5`, then enter a caller and select **Review recovery**. Its status changes once anyone finishes that burn or its attestation passes Arc block 73,321,951.
- **Image 4:** enter `0x395809d8da4a70145d5060448070a15367df722c`, choose **200 blocks**, open **History options**, choose **End time (UTC)**, enter `2026-11-20 06:44` and select **Find transfers**. The CLI gives the same range:

```sh
uv run python -m rebound.cli scan 0x395809d8da4a70145d5060448070a15367df722c --blocks 200 --end-time 2026-11-20T06:44Z
```

- **Image 6:** open `/demo` and select the 128.50 row.
