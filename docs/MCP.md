# Rebound for AI agents (MCP)

Agents that move USDC lose state. A payment agent can burn USDC on Base Sepolia, then crash, time out or lose its context before the Arc mint. When it restarts it usually knows only its wallet address, not the burn hash, and the obvious "retry" is a second burn. Rebound lets the agent resume instead: it finds the existing burn from the public address, verifies it against the source receipt, Circle's attestation and Arc's nonce, and returns the next safe step.

`rebound/mcp.py` exposes that service as a Model Context Protocol server over stdio. It adds no dependencies.

```bash
uv run python -m rebound.mcp
```

## Tools

| Tool | Wraps | Returns |
|---|---|---|
| `find_unfinished_transfers(wallet, blocks=2000, end_block?)` | `RecoveryService.scan` | Every supported transfer for the address with status, amount, fixed recipient, `next_step`, block coverage and a `partial` flag |
| `inspect_transfer(tx_hash, log_index?)` | `inspect_transaction` | Fresh verification of one burn receipt; use it to poll a waiting transfer |
| `prepare_recovery(tx_hash, log_index, caller)` | `prepare` | The **unsigned** Arc `receiveMessage` transaction, review fields, gas quote and warning |
| `check_readiness()` | `readiness` | Base Sepolia, Circle fee route and Arc Testnet provider status |

Each result has a short text summary and the same data as `structuredContent` that matches the tool's `outputSchema`. Input schemas use the same address/hash patterns and bounds as the HTTP API (`blocks` 1–50,000, `log_index` 0–100,000).

```text
agent restarts with only its wallet address
  └─ find_unfinished_transfers(wallet)
       ├─ completed                               → stop; never mint or burn again
       ├─ source_confirming / awaiting_attestation → wait, then inspect_transfer(tx_hash, log_index)
       ├─ ready                                   → prepare_recovery(...) → human or wallet reviews and signs
       └─ anything else                           → follow next_step (re-attest, original app, retry later)
```

## Safety model

- **Read-only.** No tool asks for a key, signs or broadcasts. Tools are annotated `readOnlyHint: true`. The only local write is Rebound's SQLite candidate cache (`REBOUND_DB`).
- **Unsigned only.** `prepare_recovery` returns `signed: false, broadcast: false`. A human or the caller's own wallet must review the recipient, amounts, fee and gas, then sign. Run it again right before signing and do not submit if it reports `completed`.
- **Rules in every tool description:** never burn again to "retry", the recipient is fixed, `completed` means do not mint, and waiting states mean poll later.
- **Errors are tool results.** Rejected input, provider outages, busy queues and timeouts return `isError: true` with an actionable message, so the model can recover. URLs are redacted from errors and httpx request logging stays off, so credentialed RPC URLs do not leak. Logs go to stderr only.

## Connect a client

Use the absolute path of your Rebound checkout. GUI apps may not inherit your shell `PATH`, so use the output of `which uv` as `command` if `uv` is not found.

**Codex**

```bash
codex mcp add rebound -- uv run --directory /path/to/rebound python -m rebound.mcp
```

**Claude Code**

```bash
claude mcp add rebound -- uv run --directory /path/to/rebound python -m rebound.mcp
```

**Claude Desktop** (`claude_desktop_config.json`) and **Cursor** (`.cursor/mcp.json` in a project, or `~/.cursor/mcp.json`) use the same shape:

```json
{
  "mcpServers": {
    "rebound": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/rebound", "python", "-m", "rebound.mcp"],
      "env": {
        "BASE_SEPOLIA_RPC": "https://sepolia.base.org",
        "ARC_TESTNET_RPC": "https://rpc.testnet.arc.network"
      }
    }
  }
}
```

The server reads the same variables as the app (`BASE_SEPOLIA_RPC`, `ARC_TESTNET_RPC`, `CIRCLE_SANDBOX_URL`, `REBOUND_DB`). `REBOUND_MCP_TIMEOUT_SECONDS` (default 55) bounds each tool call so the server answers before a typical 60-second client timeout.

## Protocol notes

- Dual-era server, as defined in the [MCP versioning rules](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning). It serves MCP `2026-07-28` statelessly (per-request `_meta` and `server/discover`) and answers `initialize` for `2025-11-25`, `2025-06-18`, `2025-03-26` and `2024-11-05`. An unknown version in `initialize` gets `2025-11-25`, and an unsupported per-request version gets `-32022` with the supported list.
- Tool calls run concurrently and share the service's admission queue. A burst beyond it returns a "Busy" tool error. `notifications/cancelled` stops a call without sending a reply. At end of input, in-flight calls get up to 10 seconds to answer before the process exits.
- Check it with the official inspector: `npx -y @modelcontextprotocol/inspector --cli uv run python -m rebound.mcp --method tools/list`.
