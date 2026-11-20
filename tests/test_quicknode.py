"""QuickNode Streams discovery: evidence steps, walkthrough checks, the filter and the index-only search."""

import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from rebound import protocol as p
from rebound import quicknode as q
from rebound.clients import UpstreamError
from tests.test_protocol import WALLET, burn_log

pytest_plugins = ["tests.test_service"]

FILTER = Path(__file__).resolve().parent.parent / "streams" / "filter.js"


def stream_row(**overrides):
    row = {"discovery_source": "stream", "source_verified": True, "block_number": 61_175_721,
           "source_url": "https://sepolia.basescan.org/tx/0x" + "ab" * 32, "stream_received_at": 1_795_000_000,
           "checked_at": "2026-11-20T10:00:00+00:00", "status": "ready", "attestation_matched": True,
           "nonce_used": False, "stream_signed": True}
    return row | overrides


# --------------------------------------------------------------- evidence flow

def test_stream_transfer_shows_three_verified_steps_tied_to_its_evidence():
    steps = q.evidence_steps(stream_row(), "Ready to finish")
    assert [s.key for s in steps] == ["burn", "delivery", "status"]
    assert [s.state for s in steps] == ["verified"] * 3
    assert "61,175,721" in steps[0].detail and steps[0].url.endswith("ab" * 32)
    assert "QuickNode signature verified" in steps[1].detail and "Block hash matched" in steps[1].detail
    assert "2026" in steps[1].detail
    assert "Ready to finish" in steps[2].detail and "Circle attestation matched" in steps[2].detail
    assert "Arc nonce unused" in steps[2].detail


@pytest.mark.parametrize("source", ["rpc", "receipt", None])
def test_transfers_not_found_by_a_stream_get_no_quicknode_claim(source):
    assert q.evidence_steps(stream_row(discovery_source=source), "Ready to finish") == []


def test_simulated_delivery_is_labelled_simulated_everywhere():
    steps = q.evidence_steps({"discovery_source": "example_stream", "example": True, "block_number": 9}, "Ready")
    assert [s.state for s in steps] == ["simulated"] * 3
    assert "Not a QuickNode event" in steps[1].detail


def test_unverified_source_or_unfinished_checks_stay_pending():
    unverified = q.evidence_steps(stream_row(source_verified=False, status="unavailable"), "Unavailable")
    assert [s.state for s in unverified] == ["pending", "verified", "pending"]
    unchecked = q.evidence_steps(stream_row(checked_at=None), "Waiting for Circle")
    assert unchecked[2].state == "pending"
    reorged = q.evidence_steps(stream_row(status="source_changed"), "Source changed")
    assert reorged[2].state == "pending"


def test_missing_delivery_time_still_reports_the_block_hash_match():
    steps = q.evidence_steps(stream_row(stream_received_at=None), "Ready to finish")
    assert steps[1].detail == "QuickNode signature verified. Block hash matched."


def test_unsigned_delivery_is_never_presented_as_a_quicknode_delivery():
    """A shared-secret adapter test reaches the same index but proves nothing about QuickNode."""
    row = stream_row(stream_signed=False)
    steps = q.evidence_steps(row, "Ready to finish")
    assert [s.state for s in steps] == ["verified", "pending", "verified"]
    assert "without a QuickNode signature" in steps[1].detail
    assert q.delivered_by_quicknode(row) is False
    assert q.delivered_by_quicknode(stream_row()) is True
    # Rows stored before signatures were tracked carry no flag and get the same conservative reading.
    assert q.delivered_by_quicknode({k: v for k, v in stream_row().items() if k != "stream_signed"}) is False


def test_a_transfer_found_by_history_is_never_quicknode_delivered_even_if_flagged():
    assert q.delivered_by_quicknode(stream_row(discovery_source="rpc")) is False


# ------------------------------------------------------- walkthrough checks

def checks(**overrides):
    args = {"enabled": True, "delivered": 0, "last_received": None, "transfers": []} | overrides
    return {step.key: step for step in q.walkthrough_checks(**args)}


def test_five_separate_checks_start_with_only_documented_and_simulated_evidence():
    result = checks()
    assert list(result) == ["sample", "simulated", "live", "discovery", "mint"]
    assert result["sample"].state == "observed" and result["simulated"].state == "simulated"
    assert [result[k].state for k in ("live", "discovery", "mint")] == ["pending"] * 3


def test_disabled_ingestion_is_pending_not_an_error():
    live = checks(enabled=False, delivered=3)["live"]
    assert live.state == "pending" and "not enabled" in live.detail


def test_live_delivery_needs_a_quicknode_signed_event_in_the_index():
    live = checks(delivered=2, last_received=1_795_000_000)["live"]
    assert live.state == "verified" and "2 burns" in live.detail and "QuickNode signature" in live.detail


def test_adapter_deliveries_do_not_count_as_live_stream_delivery():
    result = checks(delivered=0, local=1, transfers=[stream_row(stream_signed=False)])
    assert result["live"].state == "pending" and "not a QuickNode delivery" in result["live"].detail
    assert result["discovery"].state == "pending" and result["mint"].state == "pending"


def test_discovery_and_mint_are_not_implied_by_each_other_or_by_simulations():
    only_rpc = checks(delivered=1, transfers=[stream_row(discovery_source="rpc")])
    assert only_rpc["discovery"].state == "pending"
    found = checks(delivered=1, transfers=[stream_row()])
    assert found["discovery"].state == "verified" and found["mint"].state == "pending"
    done = checks(delivered=1, transfers=[stream_row(status="completed", destination_url="https://arc/tx/0x1")])
    assert done["mint"].state == "verified" and done["mint"].url == "https://arc/tx/0x1"
    # Completed but with no Arc receipt link is not evidence of a mint.
    no_receipt = checks(delivered=1, transfers=[stream_row(status="completed", destination_url=None)])
    assert no_receipt["mint"].state == "pending"


# ------------------------------------------------------ Stream configuration

def filter_constants():
    source = FILTER.read_text()
    return {name: re.search(rf'const {name} = "([^"]+)"', source).group(1)
            for name in ("messenger", "burnTopic", "usdcTopic")}


def test_filter_constants_match_the_protocol_the_python_decoder_enforces():
    constants = filter_constants()
    assert constants["messenger"] == p.MESSENGER
    assert constants["burnTopic"] == p.BURN_TOPIC
    assert constants["usdcTopic"] == "0x" + "00" * 12 + p.USDC[2:]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_filter_keeps_only_successful_native_usdc_burns_and_the_python_decoder_accepts_them(tmp_path):
    good = burn_log()
    wrong_token = burn_log() | {"topics": [*burn_log()["topics"][:1], "0x" + "11" * 32, *burn_log()["topics"][2:]]}
    wrong_contract = burn_log() | {"address": "0x" + "22" * 20}
    block = {"receipts": [
        {"status": "0x1", "transactionHash": good["transactionHash"], "logs": [good, wrong_token, wrong_contract]},
        {"status": "0x0", "transactionHash": "0x" + "33" * 32, "logs": [good]},
    ]}
    script = f"{FILTER.read_text()}\nprocess.stdout.write(JSON.stringify(main({{data: [{json.dumps(block)}]}})));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    logs = json.loads(out)["logs"]
    assert len(logs) == 1 and logs[0]["transactionHash"] == good["transactionHash"]
    assert p.decode_burn(logs[0]).mint_recipient == WALLET


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_filter_returns_null_so_empty_blocks_are_not_delivered():
    script = f"{FILTER.read_text()}\nprocess.stdout.write(JSON.stringify(main({{data: [{{receipts: []}}]}})));"
    assert subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout == "null"


# ------------------------------------------------------- index-only discovery

def enable(service):
    service.settings = replace(service.settings, stream_secret="test-secret")


async def test_stream_search_never_scans_source_history_and_records_delivery_time(setup_service):
    service, _state = setup_service
    enable(service)
    seen = []
    original = service.base.call

    async def spy(method, *args, **kwargs):
        seen.append(method)
        return await original(method, *args, **kwargs)

    service.base.call = spy
    assert service.ingest_logs([burn_log()]) == 1
    result = await service.scan_stream(WALLET)
    assert "eth_getLogs" not in seen
    assert result["source"] == "quicknode_streams" and result["delivered"] == 1
    row = result["transfers"][0]
    assert row["discovery_source"] == "stream" and row["source_verified"] is True
    assert isinstance(row["stream_received_at"], int)


async def test_scan_history_alone_never_claims_stream_delivery(setup_service):
    service, _state = setup_service
    enable(service)
    row = (await service.scan(WALLET, blocks=200))["transfers"][0]
    assert row["discovery_source"] == "rpc" and "stream_received_at" not in row


async def test_stream_search_reports_no_block_coverage_it_cannot_prove(setup_service):
    service, _state = setup_service
    enable(service)
    service.ingest_logs([burn_log()])
    result = await service.scan_stream(WALLET)
    assert result["from_block"] is None and result["to_block"] is None
    assert "does not report block coverage" in result["coverage"]


async def test_empty_stream_search_is_not_an_error_and_makes_no_rpc_calls(setup_service):
    service, _state = setup_service
    enable(service)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("an empty index lookup must not call an RPC")

    service.base.call = forbidden
    result = await service.scan_stream(WALLET)
    assert result["transfers"] == [] and result["delivered"] == 0


async def test_disabled_ingestion_has_a_clear_message(setup_service):
    service, _state = setup_service
    with pytest.raises(ValueError, match="QuickNode Streams ingestion is not enabled"):
        await service.scan_stream(WALLET)


async def test_provider_outage_after_a_delivery_is_named_and_not_an_empty_inbox(setup_service):
    service, _state = setup_service
    enable(service)
    service.ingest_logs([burn_log()])

    async def down(*_args, **_kwargs):
        raise UpstreamError("Base Sepolia is unavailable. Retry or configure another RPC.")

    service.base.call = down
    with pytest.raises(UpstreamError, match="QuickNode delivered 1 matching burn.*could not finish verifying it.*Retry"):
        await service.scan_stream(WALLET)


async def test_reorged_delivery_is_not_shown_as_a_verified_discovery(setup_service):
    service, state = setup_service
    enable(service)
    service.ingest_logs([burn_log()])
    state["canonical"] = False
    row = (await service.scan_stream(WALLET))["transfers"][0]
    assert row["status"] == "source_changed" and row["source_verified"] is False
    steps = q.evidence_steps(row, "Source changed")
    assert steps[0].state == "pending" and steps[2].state == "pending"


def test_delivery_time_is_kept_from_the_first_delivery_and_ignored_for_other_sources(setup_service):
    service, _state = setup_service
    enable(service)
    service.ingest_logs([burn_log()])
    first = service.store.candidates(WALLET, stream_only=True)[0].received_at
    assert isinstance(first, int)
    # A later history scan saves the same burn without a Stream block hash; the Stream evidence stays.
    burn = p.decode_burn(burn_log())
    service.store.save_many([(burn, burn_log())])
    again = service.store.candidates(WALLET, stream_only=True)[0]
    assert again.from_stream and again.received_at == first
    assert service.store.stream_summary(WALLET) == {"signed": 0, "last_signed": None, "local": 1}


def test_stream_summary_counts_only_events_delivered_by_a_stream(setup_service):
    service, _state = setup_service
    burn = p.decode_burn(burn_log())
    service.store.save_many([(burn, burn_log())])  # found by history, never delivered
    assert service.store.stream_summary(WALLET) == {"signed": 0, "last_signed": None, "local": 0}
    only = service.store.candidates(WALLET)[0]
    assert only.received_at is None and only.signed is False


def test_signature_status_is_recorded_and_a_signed_delivery_is_never_downgraded(setup_service):
    service, _state = setup_service
    service.ingest_logs([burn_log()])  # shared-secret adapter delivery
    assert service.store.candidates(WALLET, stream_only=True)[0].signed is False
    service.ingest_logs([burn_log()], signed=True)  # the same burn, later signed by QuickNode
    assert service.store.candidates(WALLET, stream_only=True)[0].signed is True
    service.ingest_logs([burn_log()])  # a later unsigned retry must not undo that
    assert service.store.candidates(WALLET, stream_only=True)[0].signed is True
    summary = service.store.stream_summary(WALLET)
    assert (summary["signed"], summary["local"]) == (1, 0) and isinstance(summary["last_signed"], int)


async def test_search_rows_carry_the_signature_status(setup_service):
    service, _state = setup_service
    enable(service)
    service.ingest_logs([burn_log()])
    row = (await service.scan_stream(WALLET))["transfers"][0]
    assert row["stream_signed"] is False and q.delivered_by_quicknode(row) is False
    service.ingest_logs([burn_log()], signed=True)
    row = (await service.scan_stream(WALLET))["transfers"][0]
    assert row["stream_signed"] is True and q.delivered_by_quicknode(row) is True


def test_stream_status_reads_the_index_and_separates_signed_from_adapter_deliveries(setup_service):
    service, _state = setup_service
    off = {"wallet": WALLET, "enabled": False, "delivered": 0, "last_received": None, "local": 0}
    assert service.stream_status(WALLET) == off
    enable(service)
    service.ingest_logs([burn_log()])
    status = service.stream_status(WALLET)
    assert status["enabled"] and (status["delivered"], status["local"]) == (0, 1)
    service.ingest_logs([burn_log()], signed=True)
    status = service.stream_status(WALLET)
    assert (status["delivered"], status["local"]) == (1, 0) and isinstance(status["last_received"], int)


async def test_webhook_marks_only_quicknode_signed_deliveries_as_signed(setup_service):
    """Through the real endpoint: a signature-verified delivery is signed, a shared-secret one is not."""
    import hmac
    import time

    import httpx

    from rebound.app import create_app
    service, _state = setup_service
    body = json.dumps({"data": [{"logs": [burn_log()]}]}).encode()

    # Shared-secret mode (no QuickNode token): accepted, but never marked as signed.
    service.settings = replace(service.settings, stream_secret="adapter-secret")
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/streams", content=body, headers={"x-rebound-secret": "adapter-secret"})
                ).json()["accepted"] == 1
    assert service.store.candidates(WALLET, stream_only=True)[0].signed is False

    # Signed mode: the HMAC is verified before ingestion, and only then is the delivery marked signed.
    token, nonce, timestamp = "quicknode-token", "nonce-1", int(time.time())
    service.settings = replace(service.settings, stream_security_token=token)
    signature = hmac.digest(token.encode(), f"{nonce}{timestamp}".encode() + body, "sha256").hex()
    headers = {"x-qn-nonce": nonce, "x-qn-timestamp": str(timestamp), "x-qn-signature": signature}
    app = create_app(service, include_ui=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        bad = headers | {"x-qn-signature": "0" * 64}
        assert (await client.post("/api/streams", content=body, headers=bad)).status_code == 401
        assert service.store.candidates(WALLET, stream_only=True)[0].signed is False
        assert (await client.post("/api/streams", content=body, headers=headers)).json()["accepted"] == 1
    assert service.store.candidates(WALLET, stream_only=True)[0].signed is True


def test_walkthrough_wallet_must_be_a_valid_public_address():
    from rebound.service import Settings
    assert Settings().quicknode_demo_wallet == "0x395809d8da4a70145d5060448070a15367df722c"
    with pytest.raises(ValueError):
        Settings(quicknode_demo_wallet="not-an-address")


# ---------------------------------------------------------------- inbox screen

def screen_text(control) -> str:
    """Every visible string under a Flet control, so tests read the screen rather than its layout."""
    found = []

    def walk(node, depth=0):
        if node is None or depth > 40:
            return
        if isinstance(node, str):
            found.append(node)
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child, depth + 1)
            return
        for name in ("value", "text", "label", "content", "controls", "title", "subtitle", "actions"):
            attribute = getattr(node, name, None)
            if attribute is not None and attribute is not node:
                walk(attribute, depth + 1)

    walk(control)
    return " | ".join(found)


def inbox(service=None, *, width=1440):
    from types import SimpleNamespace

    from rebound import ui
    return ui.InboxUI(SimpleNamespace(width=width), service or SimpleNamespace(settings=SimpleNamespace(
        quicknode_demo_wallet="0x395809d8da4a70145d5060448070a15367df722c"), stream_status=lambda wallet: {}))


def test_quicknode_search_is_labelled_and_next_to_the_main_lookup():
    text = screen_text(inbox().scan_panel())
    assert "Find transfers via QuickNode" in text and "QuickNode walkthrough" in text
    # The old buried label is gone, and only the one search entry point exists.
    assert "Search Stream index" not in text


def test_quicknode_search_stays_available_when_ingestion_is_off_so_the_reason_can_be_shown():
    from types import SimpleNamespace
    app = inbox(SimpleNamespace(settings=SimpleNamespace(stream_enabled=False)))
    assert "Find transfers via QuickNode" in screen_text(app.scan_panel())


def test_synthetic_demo_does_not_offer_the_live_quicknode_search():
    app = inbox()
    app.state.load_examples(stream=True)
    assert "Find transfers via QuickNode" not in screen_text(app.scan_panel())


def test_inspector_shows_the_discovery_label_and_evidence_flow_only_for_stream_transfers():
    app = inbox()
    shown = screen_text(app.quicknode_evidence(stream_row(status="ready")))
    assert "Discovered via QuickNode Streams" in shown
    assert "Burn confirmed" in shown and "Delivered by QuickNode" in shown and "Transfer status verified" in shown
    assert "61,175,721" in shown and "View original burn" in shown
    assert app.quicknode_evidence(stream_row(discovery_source="rpc", status="ready")) is None
    simulated = screen_text(app.quicknode_evidence({"discovery_source": "example_stream", "example": True,
                                                   "block_number": 9, "status": "ready"}))
    assert "Simulated Stream delivery" in simulated and "Discovered via QuickNode Streams" not in simulated


def test_record_names_the_provider_for_stream_discoveries():
    shown = screen_text(inbox().record(stream_row(tx_hash="0x" + "ab" * 32, status="ready", caller="0x" + "00" * 20)))
    assert "Discovered via" in shown and "QuickNode Streams" in shown


def test_unsigned_stream_delivery_is_labelled_as_an_endpoint_test_not_quicknode():
    app = inbox()
    row = stream_row(tx_hash="0x" + "ab" * 32, status="ready", caller="0x" + "00" * 20, stream_signed=False)
    record = screen_text(app.record(row))
    assert "Stream endpoint (no QuickNode signature)" in record and "QuickNode Streams |" not in record
    evidence = screen_text(app.quicknode_evidence(row))
    assert "Stream endpoint test (no QuickNode signature)" in evidence
    assert "Discovered via QuickNode Streams" not in evidence


def test_empty_stream_result_explains_what_was_and_was_not_searched():
    from rebound import ui
    message = ui.empty_result_message("stream", 2000)
    assert "delivered to the Stream index" in message and "did not scan" in message


def test_blank_walkthrough_wallet_in_env_falls_back_to_the_default(monkeypatch):
    from rebound.service import Settings
    monkeypatch.setenv("REBOUND_QUICKNODE_DEMO_WALLET", "")
    assert Settings.from_env().quicknode_demo_wallet == Settings().quicknode_demo_wallet
    monkeypatch.setenv("REBOUND_QUICKNODE_DEMO_WALLET", "0x" + "11" * 20)
    assert Settings.from_env().quicknode_demo_wallet == "0x" + "11" * 20
