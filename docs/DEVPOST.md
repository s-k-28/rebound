# Rebound submission draft

## Elevator pitch

Find interrupted USDC transfers by wallet address and verify whether they reached Arc.

## What it does

Rebound is an inbox for Base Sepolia to Arc Testnet USDC transfers. It finds supported burns from a public wallet address, matches the original Circle CCTP message, and checks whether Arc has already minted it. For an eligible direct transfer, Rebound prepares the original mint for local review and signing.

## Why we built it

A sender can close the bridge, lose the transaction hash, and still need to know where the transfer stands. Rebound reconstructs the evidence from public chain state. The inbox shows which step is waiting and when the destination has already received the mint.

## How it works

The Python service finds CCTP burns on Base Sepolia, verifies their source receipts and matches Circle’s attested messages. It checks the nonce through Arc’s MessageTransmitterV2 contract and links a matching destination receipt when available. The historical demo reads the nonce before and after a real mint and simulates the same call at the earlier block. Flet renders the inbox. SQLite stores candidate events.

## What makes it different

Discovery starts with a public wallet address and a displayed history range. The user can share an exact burn log as a live case that another person can recheck. For Circle-forwarded transfers with an app hook, the app shows proof of the CCTP mint and identifies the downstream step for the original app.

## What we would build next

Verify a controlled testnet recovery from source burn through destination mint, extend wallet history with a live index, and add a wallet-based signing handoff.

## Entry details

| Field | Draft |
|---|---|
| Main track | DeFi and Onchain Applications |
| Chain award | Best Use of Arc |
| Repository | [s-k-28/rebound](https://github.com/s-k-28/rebound) |
| Contracts and economics | [Technical README](../README.md#contracts-used) |
| Demo evidence | [Read-only historical replay](VALIDATION.md#reproduce-the-read-only-check) |
| Video | Pending verification |
| QuickNode Streams | Add after observing a real Stream delivery |
