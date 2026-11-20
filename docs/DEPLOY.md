# Public preview

The repository Dockerfile runs the Flet interface and API as a non-root user. It listens on `$PORT` when a host provides one and on 8765 otherwise.

## Render free web service

1. Render Dashboard → **New** → **Blueprint** → connect `s-k-28/rebound`. Render reads `render.yaml`.
2. Apply. Render checks `/api/health` after the first build.
3. Verify the public URL:

```sh
uv run python scripts/check_preview.py https://<service>.onrender.com
```

No secrets are required. Defaults use the public Base Sepolia RPC, the public Arc Testnet RPC and Circle's sandbox attestation API. To use private RPCs, add `BASE_SEPOLIA_RPC` and `ARC_TESTNET_RPC` as Render environment variables; they stay server-side.

Uvicorn uses its default trusted proxy address on local deployments. For an additional reverse proxy with a known private IP or subnet, set `FORWARDED_ALLOW_IPS` to that address or subnet. [Uvicorn settings](https://www.uvicorn.org/settings/#http)

## Free-tier behavior

- The service sleeps after about 15 minutes without traffic and takes roughly a minute to wake. Open the URL shortly before a judge or demo session.
- The filesystem is ephemeral, so the SQLite candidate index resets on each deploy or restart. Rebound treats the index as discovery hints only and re-verifies every transfer against the chain, so a reset only means rescanning.
- Signing happens in the local recovery CLI. Keep signing keys out of the host.

## Other hosts

Any Docker host that injects `PORT` works the same way: Railway, Fly.io, Koyeb, or Cloud Run with `--port` mapped. Locally:

```sh
docker build -t rebound .
docker run --rm -p 8765:8765 rebound          # http://127.0.0.1:8765
docker run --rm -e PORT=10000 -p 8080:10000 rebound
```
