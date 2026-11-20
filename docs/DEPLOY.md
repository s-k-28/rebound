# Public preview

The repository Dockerfile runs the full app (Flet interface, API and `/finish` page when present) as a non-root user. It listens on `$PORT` when a host provides one and on 8765 otherwise.

## Render (free web service, about 5 minutes)

1. Render Dashboard → **New** → **Blueprint** → connect `s-k-28/rebound`. Render reads `render.yaml`.
2. Apply. The first build takes about 2 minutes. Render health-checks `/api/health`.
3. Verify the public URL:

```sh
uv run python scripts/check_preview.py https://<service>.onrender.com
```

No secrets are required. Defaults use the public Base Sepolia RPC, the public Arc Testnet RPC and Circle's sandbox attestation API. To use private RPCs, add `BASE_SEPOLIA_RPC` and `ARC_TESTNET_RPC` as Render environment variables; they stay server-side.

## Free-tier behavior

- The service sleeps after about 15 minutes without traffic and takes roughly a minute to wake. Open the URL shortly before a judge or demo session.
- The filesystem is ephemeral, so the SQLite candidate index resets on each deploy or restart. Rebound treats the index as discovery hints only and re-verifies every transfer against the chain, so a reset only means rescanning.
- Keep signing keys out of the host. The browser wallet signs in the user's wallet, and the CLI signs locally.

## Other hosts

Any Docker host that injects `PORT` works the same way: Railway, Fly.io, Koyeb, or Cloud Run with `--port` mapped. Locally:

```sh
docker build -t rebound .
docker run --rm -p 8765:8765 rebound          # http://127.0.0.1:8765
docker run --rm -e PORT=10000 -p 8080:10000 rebound
```
