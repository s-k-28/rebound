# Rebound UBC Six-Hour Implementation Plan

**Goal**  
Produce a replayable DeFi and Onchain Applications entry with evidence for Best Use of Arc, a working demo, and a 2–3 minute video.

**Architecture**  
Use the Python/Flet inbox and local recovery command. Demonstrate discovery from a public wallet, confirmation of the Circle message on Base Sepolia, and verification of the matching Arc mint. Freeze the current feature set while validating the release.

**Starting point**  
Commit `febb059` passed 99 tests and a historical replay against live Arc Testnet state. Confirm the release from a fresh database. [Product spec](../../PRD.md) · [Validation evidence](../../VALIDATION.md) · [Official hackathon guide](https://docs.google.com/document/d/1QaySCo7u3VxMVNdk3C5Ab3ewc2E46AuLntti-Uzx5EY/edit)

## First 25 minutes — Eligibility and release baseline

- [ ] Check the signed-in Devpost account, invitation status, team roster, age-of-majority requirement, and November 21 in-person prize requirement. Confirm the noon CST November 20 cutoff.
  - Devpost shows an active registration, a draft with four teammates, and saved DeFi and Arc selections. Team eligibility and attendance confirmation are pending.
- [ ] Use the email supplied in chat for necessary service setup. Keep account details out of the repository. If sign-in requires interaction that is unavailable, continue with the next verified path.
- [x] Run `uv run --locked pytest`, `uv run --locked ruff check .`, and `git diff --check`. Confirm the public repository revision is current.
- [x] Open the wallet scan and exact case link from a fresh browser session. Record the source log index and Arc receipt in [VALIDATION.md](../../VALIDATION.md).

## Minutes 25–90 — Prove the recovery sequence

- [ ] Check a dedicated testnet wallet for Base Sepolia USDC, Base Sepolia ETH gas, and Arc native USDC gas. Use the official Circle and Base faucet links when funding is needed. [Circle faucet](https://faucet.circle.com/) · [Base testnet funding](https://docs.base.org/get-started/get-funds)
- [ ] For an authorized, funded testnet wallet, make one small manual CCTP burn to a controlled Arc recipient. Preserve the source block and receipt, then stop before minting.
- [ ] Discover the transfer with only the public address. Verify the original recipient, matching signed message, unused Arc nonce, and native USDC gas budget.
- [ ] Complete one approved testnet mint. Refresh the inbox and verify the matching receipt. Run `prepare` again on the same source log and capture the completed-state refusal.
- [ ] If wallet funding or signing is unavailable, record the live read-only case. Replay Arc blocks 73,127,413 and 73,127,414 and identify the historical simulation in the video.

## Minutes 90–145 — Package a working demo

- [x] Test the GitHub revision on a clean local launch, including the exact case URL and browser WebSocket. Verify desktop and narrow layouts.
- [ ] Try a free Render Docker web service if the account and GitHub connection are available. Verify HTTPS, `/api/health`, and a live case in a signed-out browser. Check for cold-start and ephemeral storage behavior. [Render deployment guide](https://render.com/docs/your-first-deploy) · [Free service limits](https://render.com/docs/free)
- [x] If managed hosting stalls, use a Cloudflare Quick Tunnel for a temporary rehearsal or record the working local app with the public repository and setup steps. The temporary preview passed the public HTTPS replay check. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

## Minutes 145–180 — Validate the optional QuickNode bounty

- [ ] Time-box the setup. Use Base Sepolia, the Block with Receipts dataset, `streams/filter.js`, and the signed HTTPS webhook. Backfill a small range containing the known source burn.
- [ ] Save the actual Stream ID, filter revision, delivery range, and successful webhook evidence. Find the burn with **Search Stream index** and show its Stream provenance. Select the bounty only when this path succeeds. [QuickNode setup](../../QUICKNODE.md)
  - The signed sample passed according to the approved webhook test PR. Discovery from a live Stream-delivered burn is still pending. [PR #1](https://github.com/s-k-28/rebound/pull/1)

## Minutes 180–280 — Record and review the pitch

- [x] Draft the Devpost answers, covering the problem, method, differentiation, and next iteration. Explain address-first discovery when the original app or hash is unavailable. [Submission draft](../../DEVPOST.md)
- [x] Record the 2–3 minute demo. The 2:35 local cut shows live wallet discovery, a historical replay, and the matching Arc receipt. Publication and signed-out access are separate checks below.
- [ ] Rehearse the four-minute finalist pitch. Include one source receipt, one Arc receipt, the original recipient, the CCTP fee and Arc gas, and an honest explanation of any historical footage.
- [ ] Verify the video link in a signed-out browser and confirm the repository, architecture, economic assumptions, and contract addresses are linked.

## Minutes 280–360 — Freeze and verify

- [ ] Fix only issues that affect eligibility, installing the repo, opening the demo, or reproducing a claim. Commit each verified fix.
- [ ] Check the team roster, exactly one main track, Arc chain award, prize opt-ins, and evidence for each claim. Leave QuickNode out unless live delivery was recorded.
- [ ] Confirm the published link, video duration and visibility, and final commit hash. Retest every judge-facing link.
- [ ] Submit only after eligibility is established and the team has authorized its statements and the submission. Preserve the reviewed packet if eligibility or attendance cannot be established.
