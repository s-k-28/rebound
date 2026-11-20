"""UTC discovery endpoints remain bounded and preserve legacy scan call shapes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from rebound import cli, mcp
from rebound.app import create_app
from tests.test_mcp import SCAN, FakeService, call_result
from tests.test_protocol import WALLET

pytest_plugins = ["tests.test_service"]
TIMES = ["2026-11-20 07:30", "2026-11-20T07:30:00Z", "2026-11-20T07:30Z"]


@pytest.mark.parametrize("when", TIMES)
async def test_scan_api_forwards_bounded_utc_time(setup_service, when):
    service, _ = setup_service
    service.scan = AsyncMock(return_value=SCAN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, include_ui=False)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/scan", params={"wallet": WALLET, "blocks": 150, "end_time": when}
        )
    assert response.status_code == 200
    service.scan.assert_awaited_once_with(WALLET, blocks=150, end_block=None, end_time=when)


async def test_scan_api_preserves_unset_time_and_rejects_conflicting_endpoint(setup_service):
    service, _ = setup_service
    service.scan = AsyncMock(return_value=SCAN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, include_ui=False)),
        base_url="http://test",
    ) as client:
        assert (await client.get("/api/scan", params={"wallet": WALLET})).status_code == 200
        service.scan.assert_awaited_once_with(WALLET, blocks=2000, end_block=None)
        service.scan.reset_mock()
        bad = await client.get(
            "/api/scan", params={"wallet": WALLET, "end_block": 100, "end_time": TIMES[0]}
        )
        assert bad.status_code == 400
        assert service.scan.await_count == 0
        for invalid in ["", "x" * 100, "2026"]:
            response = await client.get("/api/scan", params={"wallet": WALLET, "end_time": invalid})
            assert response.status_code == 422
        assert service.scan.await_count == 0


@pytest.mark.parametrize("when", TIMES)
async def test_scan_cli_forwards_optional_end_time(monkeypatch, when):
    scan = AsyncMock(return_value=SCAN)
    monkeypatch.setattr("rebound.service.RecoveryService", lambda *_: SimpleNamespace(scan=scan))
    args = cli.parser().parse_args(["scan", WALLET, "--blocks", "150", "--end-time", when])
    assert await cli.run(args) == SCAN
    scan.assert_awaited_once_with(WALLET, blocks=150, end_block=None, end_time=when)


async def test_scan_cli_preserves_legacy_namespace_without_time(monkeypatch):
    scan = AsyncMock(return_value=SCAN)
    monkeypatch.setattr("rebound.service.RecoveryService", lambda *_: SimpleNamespace(scan=scan))
    await cli.run(SimpleNamespace(command="scan", wallet=WALLET, blocks=150, end_block=100))
    scan.assert_awaited_once_with(WALLET, blocks=150, end_block=100)


@pytest.mark.parametrize(
    "options",
    [
        ["--end-time", ""],
        ["--end-time", "x" * 100],
        ["--end-time", "2026"],
        ["--end-time", TIMES[0], "--end-block", "100"],
    ],
)
def test_scan_cli_refuses_unbounded_or_conflicting_time_before_service_work(options):
    with pytest.raises(SystemExit) as stopped:
        cli.parser().parse_args(["scan", WALLET, *options])
    assert stopped.value.code == 2


def test_scan_help_explains_utc_examples(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.parser().parse_args(["scan", "--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "--end-time" in output and "UTC" in output and "2026-11-20" in output


async def test_mcp_time_scan_forwards_only_when_set_and_rejects_conflicts():
    service = FakeService()
    service.scan = AsyncMock(return_value=SCAN)
    server = mcp.McpServer(service)
    result = await call_result(
        server, "find_unfinished_transfers", {"wallet": WALLET, "end_time": TIMES[0]}
    )
    assert result["isError"] is False
    service.scan.assert_awaited_once_with(WALLET, blocks=2000, end_block=None, end_time=TIMES[0])
    service.scan.reset_mock()
    result = await call_result(
        server,
        "find_unfinished_transfers",
        {"wallet": WALLET, "end_block": 100, "end_time": TIMES[0]},
    )
    assert result["isError"] is True and service.scan.await_count == 0
    for invalid in ["", "x" * 100, "2026"]:
        result = await call_result(
            server, "find_unfinished_transfers", {"wallet": WALLET, "end_time": invalid}
        )
        assert result["isError"] is True
    assert service.scan.await_count == 0


async def test_utc_syntax_errors_are_owned_by_backend_across_interfaces(setup_service, monkeypatch):
    service, _ = setup_service
    scan = AsyncMock(side_effect=ValueError("Enter a valid UTC date and time."))
    service.scan = scan
    invalid_date = "2026-13-20 07:30"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, include_ui=False)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/scan", params={"wallet": WALLET, "end_time": invalid_date}
        )
    assert (
        response.status_code == 400
        and response.json()["detail"] == "Enter a valid UTC date and time."
    )
    monkeypatch.setattr("rebound.service.RecoveryService", lambda *_: SimpleNamespace(scan=scan))
    args = cli.parser().parse_args(["scan", WALLET, "--end-time", invalid_date])
    with pytest.raises(ValueError, match="valid UTC"):
        await cli.run(args)
    fake = FakeService()
    fake.scan = scan
    result = await call_result(
        mcp.McpServer(fake),
        "find_unfinished_transfers",
        {"wallet": WALLET, "end_time": invalid_date},
    )
    assert result["isError"] is True and scan.await_count == 3


async def test_partial_scan_does_not_claim_the_candidate_cap_was_the_only_cause():
    service = FakeService()
    service.scan = AsyncMock(return_value=SCAN | {"partial": True, "transfers": []})
    result = await call_result(
        mcp.McpServer(service), "find_unfinished_transfers", {"wallet": WALLET}
    )
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "provider retention" in text and "reported block range" in text
    assert "more than 100 candidates matched" not in text
    assert result["structuredContent"]["partial"] is True
