"""Small, asynchronous upstream clients with credential-safe errors."""

import asyncio
import itertools
import time

import httpx


class UpstreamError(RuntimeError):
    """A provider failed; deliberately excludes URLs, response bodies and secrets."""


class RangeLimitError(UpstreamError):
    """A log range must be subdivided; no range is silently omitted."""


class Rpc:
    METHODS = frozenset({
        "eth_chainId", "eth_getCode", "eth_blockNumber", "eth_getBlockByNumber",
        "eth_getLogs", "eth_getTransactionReceipt", "eth_call", "eth_estimateGas",
        "eth_getTransactionCount", "eth_gasPrice", "eth_sendRawTransaction",
        "eth_getBalance",
    })

    def __init__(self, url: str, label: str, client: httpx.AsyncClient):
        self.url, self.label, self.client = url, label, client
        self._ids = itertools.count(1)
        self._log_lock = asyncio.Lock()
        self._last_log_request = 0.0

    async def call(self, method: str, params: list | None = None):
        if method not in self.METHODS:
            raise ValueError("Unsupported RPC method.")
        # Never automatically repeat a broadcast, including after a transport error.
        attempts = 1 if method == "eth_sendRawTransaction" else 3
        for attempt in range(attempts):
            if method == "eth_getLogs":
                async with self._log_lock:
                    await asyncio.sleep(max(0.0, 0.15 - (time.monotonic() - self._last_log_request)))
                    self._last_log_request = time.monotonic()
            request_id = next(self._ids)
            try:
                response = await self.client.post(self.url, json={
                    "jsonrpc": "2.0", "id": request_id, "method": method, "params": params or [],
                }, timeout=20)
                if method == "eth_getLogs" and response.status_code == 413:
                    raise RangeLimitError(f"{self.label} requires a smaller log range.")
                if (response.status_code == 429 or response.status_code >= 500) and attempt + 1 < attempts:
                    try:
                        delay = min(3.0, max(0.0, float(response.headers.get("Retry-After", 1 + attempt))))
                    except ValueError:
                        delay = 1 + attempt
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                data = response.json()
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise UpstreamError(f"{self.label} is unavailable. Retry or configure another RPC.") from exc
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise UpstreamError(f"{self.label} is unavailable. Retry or configure another RPC.") from exc
        if isinstance(data, dict) and method == "eth_getLogs" and isinstance(data.get("error"), dict) and data["error"].get("code") in (-32614, -32005):
            raise RangeLimitError(f"{self.label} requires a smaller log range.")
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            suffix = " Try a shorter history window or a dedicated RPC." if method == "eth_getLogs" else " Retry later."
            raise UpstreamError(f"{self.label} could not complete {method}.{suffix}")
        return data["result"]


class Circle:
    def __init__(self, url: str, client: httpx.AsyncClient):
        self.url, self.client = url.rstrip("/"), client

    async def messages(self, tx_hash: str) -> dict:
        try:
            response = await self.client.get(
                f"{self.url}/v2/messages/6", params={"transactionHash": tx_hash}, timeout=20,
            )
            if response.status_code == 404:
                return {"messages": [], "pending": True}
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise UpstreamError("Circle's attestation service is unavailable. Refresh later.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise UpstreamError("Circle returned an unreadable transfer status. Refresh later.")
        return data


def quantity(value, label: str = "RPC") -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise UpstreamError(f"{label} returned an invalid numeric result.")
    try:
        result = int(value, 16)
    except ValueError as exc:
        raise UpstreamError(f"{label} returned an invalid numeric result.") from exc
    if result < 0:
        raise UpstreamError(f"{label} returned an invalid numeric result.")
    return result
