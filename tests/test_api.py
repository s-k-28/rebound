import httpx

from tests.test_protocol import WALLET

pytest_plugins = ["tests.test_service"]


async def test_invalid_scan_is_rejected_before_any_upstream_work(setup_service):
    from rebound.app import create_app
    service, _ = setup_service
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/scan", params={"wallet": "not-a-wallet"})).status_code == 422
        assert (await client.get("/api/scan", params={"wallet": WALLET, "blocks": 50001})).status_code == 422
        assert (await client.get("/api/scan", params={"wallet": WALLET, "end_block": -1})).status_code == 422


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

    service, state = setup_service
    state["rpc_error"] = True
    app = create_app(service, include_ui=False)
    url = "/api/transactions/0x" + "cd" * 32
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(url)
        assert result.status_code == 200
        rows = result.json()["transfers"]
        assert [(row["log_index"], row["discovery_source"], row["status"]) for row in rows] == [(3, "receipt", "ready")]
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
