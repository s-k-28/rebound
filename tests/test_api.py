import asyncio

import httpx
import pytest

from tests.test_protocol import WALLET

pytest_plugins = ["tests.test_service"]


async def test_readiness_reports_individual_providers_and_caches_recent_checks(setup_service):
    from rebound.app import create_app

    service, state = setup_service
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/health")).status_code == 200
        first = await client.get("/api/ready")
        assert first.status_code == 200
        assert first.json()["providers"] == {"base_sepolia": "ok", "circle_cctp_fee_route": "ok", "arc_testnet": "ok"}
        assert (await client.get("/api/ready")).json() == first.json()
        assert state["fee_checks"] == 1

        state["arc_chain"] = 1
        state["fees"] = []
        service._readiness_expires = 0
        next_check = await client.get("/api/ready")
        assert next_check.status_code == 503
        assert next_check.json()["providers"] == {
            "base_sepolia": "ok", "circle_cctp_fee_route": "unavailable", "arc_testnet": "unavailable",
        }
        assert next_check.headers["cache-control"] == "no-store"
        assert (await client.get("/api/health")).status_code == 200


async def test_invalid_scan_is_rejected_before_any_upstream_work(setup_service):
    from rebound.app import create_app
    service, _ = setup_service
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/scan", params={"wallet": "not-a-wallet"})).status_code == 422
        assert (await client.get("/api/scan", params={"wallet": WALLET, "blocks": 50001})).status_code == 422
        assert (await client.get("/api/scan", params={"wallet": WALLET, "end_block": -1})).status_code == 422


async def test_browser_and_api_share_admission_and_cancellation_releases_queue_space(setup_service):
    from rebound.app import create_app

    service, _ = setup_service
    service._admission_limit = 1
    await service._scan_slots.acquire()
    await service._scan_slots.acquire()
    browser_scan = asyncio.create_task(service.scan(WALLET, blocks=150))
    await asyncio.sleep(0)
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/scan", params={"wallet": WALLET, "blocks": 150})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "5"
        browser_scan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await browser_scan
        service._scan_slots.release()
        service._scan_slots.release()
        assert (await client.get("/api/scan", params={"wallet": WALLET, "blocks": 150})).status_code == 200


async def test_api_preserves_real_discovery_and_disabled_stream_semantics(setup_service):
    from rebound.app import create_app
    service, _ = setup_service
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/api/scan", params={"wallet": WALLET, "blocks": 150})
        assert result.status_code == 200
        assert result.json()["transfers"][0]["status"] == "ready"
        fixed = await client.get("/api/scan", params={"wallet": WALLET, "blocks": 20, "end_block": 101})
        assert fixed.status_code == 200
        assert (fixed.json()["from_block"], fixed.json()["to_block"]) == (82, 101)
        assert (await client.post("/api/streams", json={"logs": []})).status_code == 503


async def test_prepare_failure_is_client_actionable_without_provider_secret(setup_service):
    from rebound.app import create_app
    service, state = setup_service
    state["used"] = True
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/api/prepare", json={"tx_hash": "0x" + "cd" * 32, "log_index": 3, "caller": WALLET})
        assert result.status_code == 400
        assert "completed" in result.json()["detail"]


async def test_transaction_lookup_reuses_canonical_receipt_without_a_history_scan(setup_service):
    from rebound.app import create_app
    from tests.test_protocol import burn_log

    service, state = setup_service
    state["rpc_error"] = True
    state["logs"].append(burn_log(amount=10_000_000) | {"logIndex": "0x4"})
    app = create_app(service, include_ui=False)
    url = "/api/transactions/0x" + "cd" * 32
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(url, params={"log_index": 3})
        assert result.status_code == 200
        rows = result.json()["transfers"]
        assert [(row["log_index"], row["discovery_source"], row["status"]) for row in rows] == [(3, "receipt", "ready")]
        assert rows[0]["source_verified"] and rows[0]["attestation_matched"]
        assert rows[0]["nonce_used"] is False and rows[0]["destination_block"] == 900
        assert rows[0]["expiration_block"] == 1000
        assert (await client.get(url, params={"log_index": 99})).json()["transfers"] == []
        state["logs"] = []
        assert (await client.get(url)).json()["transfers"] == []
        state["canonical"] = False
        result = await client.get(url)
        assert result.status_code == 400
        assert "canonical block" in result.json()["detail"]


async def test_authenticated_stream_envelopes_are_candidates_only(setup_service):
    from dataclasses import replace

    from rebound.app import create_app
    from tests.test_protocol import burn_log
    service, state = setup_service
    service.settings = replace(service.settings, stream_secret="test-secret")
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        params = {"wallet": WALLET}
        history = await client.get("/api/scan", params=params)
        assert history.json()["transfers"][0]["discovery_source"] == "rpc"
        assert (await client.get("/api/stream-index", params=params)).json()["transfers"] == []
        assert (await client.post("/api/streams", json={"logs": []})).status_code == 401
        for data in [{"logs": [burn_log()]}, {"data": {"logs": [burn_log()]}, "metadata": {}},
                     {"data": [{"logs": [burn_log()]}], "metadata": {}}]:
            response = await client.post("/api/streams", json=data, headers={"x-rebound-secret": "test-secret"})
            assert response.status_code == 200
            assert response.json()["accepted"] == 1
            assert "Candidates only" in response.json()["note"]
        history = await client.get("/api/scan", params=params)
        assert history.json()["transfers"][0]["discovery_source"] == "stream"

        state["rpc_error"] = True
        index = await client.get("/api/stream-index", params=params)
        assert index.status_code == 200
        assert index.json()["from_block"] is None
        assert [(row["discovery_source"], row["status"]) for row in index.json()["transfers"]] == [("stream", "ready")]
        state["canonical"] = False
        index = await client.get("/api/stream-index", params=params)
        assert index.json()["transfers"][0]["status"] == "source_changed"


async def test_quicknode_signature_verifies_the_decoded_payload_and_blocks_fallback(setup_service):
    import gzip
    import hmac
    import json
    import time
    from dataclasses import replace

    from rebound.app import create_app
    from tests.test_protocol import burn_log

    service, _ = setup_service
    token = "quicknode-test-token"
    service.settings = replace(service.settings, stream_security_token=token, stream_secret="legacy-test-secret")
    app = create_app(service, include_ui=False)
    body = json.dumps({"data": [{"logs": [burn_log()]}]}, indent=2).encode()
    nonce = "delivery-test-nonce"

    def signed(timestamp: int) -> dict:
        signature = hmac.digest(token.encode(), f"{nonce}{timestamp}".encode() + body, "sha256").hex()
        return {"x-qn-nonce": nonce, "x-qn-timestamp": str(timestamp), "x-qn-signature": signature}

    headers = signed(int(time.time()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/streams"
        assert (await client.post(url, content=body, headers={"x-rebound-secret": "legacy-test-secret"})).status_code == 401
        unsigned = await client.post(url, content=b"bad gzip", headers={"Content-Encoding": "gzip"})
        assert unsigned.status_code == 401
        assert (await client.post(url, content=body + b" ", headers=headers)).status_code == 401
        assert (await client.post(url, content=body, headers=signed(int(time.time()) - 1000))).status_code == 401
        assert (await client.get("/api/stream-index", params={"wallet": WALLET})).json()["transfers"] == []

        assert (await client.post(url, content=body, headers=headers)).json()["accepted"] == 1
        compressed = headers | {"Content-Encoding": "gzip"}
        assert (await client.post(url, content=gzip.compress(body), headers=compressed)).json()["accepted"] == 1
        assert (await client.post(url, content=b"bad gzip", headers=compressed)).status_code == 400
        assert (await client.post(url, content=gzip.compress(b"x" * 2_000_001), headers=compressed)).status_code == 413
        index = await client.get("/api/stream-index", params={"wallet": WALLET})
        assert [row["discovery_source"] for row in index.json()["transfers"]] == ["stream"]
