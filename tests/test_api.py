import asyncio

import httpx
import pytest

from tests.test_protocol import WALLET

pytest_plugins = ["tests.test_service"]


@pytest.mark.parametrize("signed", [False, True])
async def test_stream_slots_apply_before_body_reads_and_release_after_success(setup_service, signed):
    import hmac
    import time
    from dataclasses import replace

    from rebound.app import STREAM_MAX_IN_FLIGHT, create_app

    recovery, _ = setup_service
    token, secret = "quicknode-test-token", "local-test-secret"
    recovery.settings = replace(recovery.settings,
        stream_security_token=token if signed else "", stream_secret=secret)
    body = b'{"logs": []}'
    if signed:
        nonce, timestamp = "fixture-delivery", str(int(time.time()))
        headers = {"x-qn-nonce": nonce, "x-qn-timestamp": timestamp,
                   "x-qn-signature": hmac.digest(token.encode(), (nonce + timestamp).encode() + body, "sha256").hex()}
    else:
        headers = {"x-rebound-secret": secret}
    app = create_app(recovery, include_ui=False)
    release = asyncio.Event()
    started = [asyncio.Event() for _ in range(STREAM_MAX_IN_FLIGHT)]
    probe_read = asyncio.Event()

    async def held_body(event):
        event.set()
        await release.wait()
        yield body

    async def probe_body():
        probe_read.set()
        yield body

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        requests = [asyncio.create_task(client.post("/api/streams", content=held_body(event), headers=headers))
                    for event in started]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=1)
            full = await client.post("/api/streams", content=probe_body(), headers=headers)
            assert full.status_code == 429 and full.headers["retry-after"] == "5"
            assert not probe_read.is_set()
            # Authentication remains headers-first even when admission is full.
            unauthorized = await client.post("/api/streams", content=probe_body())
            assert unauthorized.status_code == 401 and not probe_read.is_set()
        finally:
            release.set()
            responses = await asyncio.gather(*requests)
        assert all(response.status_code == 200 for response in responses)
        assert (await client.post("/api/streams", content=body, headers=headers)).status_code == 200


async def test_stream_body_read_timeout_returns_408_and_releases_slot(setup_service, monkeypatch):
    from dataclasses import replace

    from rebound import app as module

    monkeypatch.setattr(module, "STREAM_MAX_IN_FLIGHT", 1)
    monkeypatch.setattr(module, "STREAM_BODY_TIMEOUT_SECONDS", 0.01)
    recovery, _ = setup_service
    recovery.settings = replace(recovery.settings, stream_secret="test-secret")
    app = module.create_app(recovery, include_ui=False)
    finished = asyncio.Event()

    async def delayed_body():
        try:
            await asyncio.Event().wait()
            yield b'{"logs": []}'
        finally:
            finished.set()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"x-rebound-secret": "test-secret"}
        response = await asyncio.wait_for(client.post("/api/streams", content=delayed_body(), headers=headers), timeout=1)
        assert response.status_code == 408 and "timed out" in response.json()["detail"]
        assert finished.is_set()
        assert (await client.post("/api/streams", json={"logs": []}, headers=headers)).status_code == 200


async def test_cancelled_stream_body_read_releases_slot(setup_service, monkeypatch):
    from dataclasses import replace

    from rebound import app as module

    monkeypatch.setattr(module, "STREAM_MAX_IN_FLIGHT", 1)
    recovery, _ = setup_service
    recovery.settings = replace(recovery.settings, stream_secret="test-secret")
    app = module.create_app(recovery, include_ui=False)
    started = asyncio.Event()

    async def delayed_body():
        started.set()
        await asyncio.Event().wait()
        yield b'{"logs": []}'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"x-rebound-secret": "test-secret"}
        request = asyncio.create_task(client.post("/api/streams", content=delayed_body(), headers=headers))
        await asyncio.wait_for(started.wait(), timeout=1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert (await client.post("/api/streams", json={"logs": []}, headers=headers)).status_code == 200


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


async def test_transaction_api_verifies_a_submitted_arc_hash_for_the_exact_source_event(submitted_arc_receipt):
    from rebound.app import create_app

    recovery, _, arc_hash, _, _ = submitted_arc_receipt
    app = create_app(recovery, include_ui=False)
    url = "/api/transactions/0x" + "cd" * 32
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(url, params={"log_index": 3, "destination_hash": arc_hash})
        mismatch = await client.get(url, params={"log_index": 3, "destination_hash": "0x" + "fe" * 32})
    assert response.status_code == 200
    row, = response.json()["transfers"]
    assert row["submitted_receipt_verified"] is True
    assert row["submitted_receipt_url"] == f"https://explorer.testnet.arc.io/tx/{arc_hash}"
    assert row["destination_url"] is None
    wrong_row, = mismatch.json()["transfers"]
    assert mismatch.status_code == 200 and wrong_row["status"] == "completed"
    assert wrong_row["submitted_receipt_verified"] is False and wrong_row["submitted_receipt_url"] is None


async def test_submitted_arc_hash_query_requires_exact_index_and_valid_hash(setup_service):
    from rebound.app import create_app

    recovery, _ = setup_service
    app = create_app(recovery, include_ui=False)
    url = "/api/transactions/0x" + "cd" * 32
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing_index = await client.get(url, params={"destination_hash": "0x" + "ae" * 32})
        assert missing_index.status_code == 400
        assert "log_index" in missing_index.json()["detail"]
        invalid_hash = await client.get(url, params={"log_index": 3, "destination_hash": "0x1234"})
        assert invalid_hash.status_code == 422


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
