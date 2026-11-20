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
    row = {"discovery_source": "stream", "source_verified": True, "source_confirmations": 12,
           "block_number": 61_175_721,
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
    assert "HMAC authentication verified" in steps[1].detail and "block hash matched" in steps[1].detail
    assert "2026" in steps[1].detail
    assert "Ready to finish" in steps[2].detail and "Circle attestation matched" in steps[2].detail
    assert "Arc nonce unused" in steps[2].detail


@pytest.mark.parametrize("source", ["rpc", "receipt", None])
def test_transfers_not_found_by_a_stream_get_no_quicknode_claim(source):
    assert q.evidence_steps(stream_row(discovery_source=source), "Ready to finish") == []


def test_simulated_delivery_is_labelled_simulated_everywhere():
    steps = q.evidence_steps({"discovery_source": "example_stream", "example": True, "block_number": 9}, "Ready")
    assert [s.state for s in steps] == ["simulated"] * 3
    assert "Synthetic endpoint delivery" in steps[1].detail


def test_unverified_source_or_unfinished_checks_stay_pending():
    unverified = q.evidence_steps(stream_row(source_verified=False, status="unavailable"), "Unavailable")
    assert [s.state for s in unverified] == ["pending", "verified", "pending"]
    unchecked = q.evidence_steps(stream_row(checked_at=None), "Waiting for Circle")
    assert unchecked[2].state == "pending"
    reorged = q.evidence_steps(stream_row(status="source_changed"), "Source changed")
    assert reorged[2].state == "pending"


def test_missing_delivery_time_still_reports_the_block_hash_match():
    steps = q.evidence_steps(stream_row(stream_received_at=None), "Ready to finish")
    assert "HMAC authentication verified" in steps[1].detail and "Received" not in steps[1].detail


def test_unsigned_delivery_is_never_presented_as_a_quicknode_delivery():
    """A shared-secret adapter test reaches the same index but proves nothing about QuickNode."""
    row = stream_row(stream_signed=False)
    steps = q.evidence_steps(row, "Ready to finish")
    assert [s.state for s in steps] == ["verified", "observed", "verified"]
    assert "No HMAC payload evidence" in steps[1].detail
    assert q.authenticated_stream_payload(row) is False
    assert q.authenticated_stream_payload(stream_row()) is True
    # Rows stored before signatures were tracked carry no flag and get the same conservative reading.
    assert q.authenticated_stream_payload({k: v for k, v in stream_row().items() if k != "stream_signed"}) is False


def test_legacy_stream_records_do_not_invent_the_authentication_method():
    row = stream_row(stream_signed=False, stream_received_at=None)
    recorded = q.evidence_steps(row, "Ready to finish")[1]
    assert recorded.title == "Stream endpoint record"
    assert "no stored receipt time or HMAC evidence" in recorded.detail


def test_a_plain_rpc_transfer_cannot_carry_stream_authentication():
    assert q.authenticated_stream_payload(stream_row(discovery_source="rpc")) is False


# ------------------------------------------------------- walkthrough checks

def checks(**overrides):
    args = {"enabled": True, "delivered": 0, "last_received": None, "transfers": []} | overrides
    return {step.key: step for step in q.walkthrough_checks(**args)}


def test_six_separate_checks_start_with_only_documented_and_simulated_evidence():
    result = checks()
    assert list(result) == ["sample", "simulated", "authenticated", "live", "discovery", "mint"]
    assert result["sample"].state == "observed" and result["simulated"].state == "simulated"
    assert [result[k].state for k in ("authenticated", "live", "discovery", "mint")] == ["pending"] * 4


def test_disabled_ingestion_is_pending_not_an_error():
    authenticated = checks(enabled=False, delivered=3)["authenticated"]
    assert authenticated.state == "pending" and "not enabled" in authenticated.detail


def test_hmac_authentication_does_not_confirm_actual_provider_delivery():
    result = checks(delivered=2, last_received=1_795_000_000)
    proof = result["authenticated"]
    assert proof.state == "verified" and "2 indexed burns" in proof.detail and "HMAC authentication" in proof.detail
    assert result["live"].state == "pending"


def test_adapter_deliveries_do_not_count_as_live_stream_delivery():
    result = checks(delivered=0, local=1, transfers=[stream_row(stream_signed=False)])
    assert result["live"].state == "pending" and result["authenticated"].state == "pending"
    assert "endpoint record" in result["authenticated"].detail
    assert result["discovery"].state == "pending" and result["mint"].state == "pending"


def test_discovery_and_mint_are_not_implied_by_each_other_or_by_simulations():
    only_rpc = checks(delivered=1, transfers=[stream_row(discovery_source="rpc")])
    assert only_rpc["discovery"].state == "pending"
    found = checks(delivered=1, transfers=[stream_row()])
    assert found["discovery"].state == "verified" and found["mint"].state == "pending"
    done = checks(delivered=1, transfers=[stream_row(status="completed", nonce_used=True,
                                                    destination_url="https://arc/tx/0x1")])
    assert done["mint"].state == "verified" and done["mint"].url == "https://arc/tx/0x1"
    # Completed but with no Arc receipt link is not evidence of a mint.
    no_receipt = checks(delivered=1, transfers=[stream_row(status="completed", nonce_used=True, destination_url=None)])
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
    assert result["source"] == "stream_index" and result["delivered"] == 1
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
    assert "Block coverage is unknown" in result["coverage"]


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
    with pytest.raises(UpstreamError, match="Stream index has 1 matching burn.*could not finish verifying it.*Retry"):
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
    assert row["stream_signed"] is False and q.authenticated_stream_payload(row) is False
    service.ingest_logs([burn_log()], signed=True)
    row = (await service.scan_stream(WALLET))["transfers"][0]
    assert row["stream_signed"] is True and q.authenticated_stream_payload(row) is True


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
    assert text.count("Search Stream index") == 1 and "QuickNode walkthrough" in text


def test_quicknode_search_stays_available_when_ingestion_is_off_so_the_reason_can_be_shown():
    from types import SimpleNamespace
    app = inbox(SimpleNamespace(settings=SimpleNamespace(stream_enabled=False)))
    assert "Search Stream index" in screen_text(app.scan_panel())


def test_synthetic_demo_does_not_offer_the_live_quicknode_search():
    app = inbox()
    app.state.load_examples(stream=True)
    assert "Search Stream index" not in screen_text(app.scan_panel())


def test_inspector_adds_endpoint_evidence_only_for_stream_transfers():
    app = inbox()
    shown = screen_text(app.quicknode_evidence(stream_row(status="ready")))
    assert "Authenticated Stream payload" in shown and "independent provider dashboard evidence" in shown
    assert "Burn confirmed" not in shown and "Transfer status verified" not in shown
    assert app.quicknode_evidence(stream_row(discovery_source="rpc", status="ready")) is None
    simulated = screen_text(app.quicknode_evidence({"discovery_source": "example_stream", "example": True,
                                                   "block_number": 9, "status": "ready"}))
    assert "Simulated Stream delivery" in simulated and "Authenticated Stream payload" not in simulated


def test_record_identifies_authenticated_stream_payloads():
    shown = screen_text(inbox().record(stream_row(tx_hash="0x" + "ab" * 32, status="ready", caller="0x" + "00" * 20)))
    assert "Discovered via" in shown and "Authenticated Stream payload" in shown


def test_unsigned_stream_delivery_is_labelled_as_an_endpoint_test_not_quicknode():
    app = inbox()
    row = stream_row(tx_hash="0x" + "ab" * 32, status="ready", caller="0x" + "00" * 20, stream_signed=False)
    record = screen_text(app.record(row))
    assert "Stream endpoint record" in record and "Authenticated Stream payload" not in record
    evidence = screen_text(app.quicknode_evidence(row))
    assert "Shared-secret adapter payload" in evidence
    assert "Authenticated Stream payload" not in evidence


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


# ---------------------------------------------------------- recorded Stream

ROOT = Path(__file__).resolve().parent.parent
STREAM_ID = "c797f5ca-9aa8-4874-b8f1-a291a4066f27"


def test_recorded_stream_agrees_with_the_filter_the_route_and_the_controlled_burn():
    record = q.load_stream_record()
    assert record is not None and record["id"] == STREAM_ID
    assert (ROOT / record["filter"]).is_file()
    assert record["network"] == "Base Sepolia" and record["dataset_id"] == "block_with_receipts"
    assert f"Dataset: {record['network']}, {record['dataset_id']}" in FILTER.read_text()
    # The destination path must be the route Rebound actually serves.
    from rebound.app import create_app
    routes = {route.path for route in create_app(include_ui=False).routes}
    assert record["destination_path"] in routes
    assert record["start_block"] <= record["controlled_burn_block"]
    seen = record["observed"]
    assert seen["blocks_sent"] <= seen["blocks_processed"] and seen["last_block_delivered"] >= record["start_block"]


def test_stream_record_holds_no_credentials_and_no_destination_host():
    text = (ROOT / "rebound" / "stream_record.json").read_text().lower()
    for forbidden in ("http", "app.github.dev", "token\":", "secret", "password", "apikey", "api_key"):
        assert forbidden not in text, forbidden


@pytest.mark.parametrize("content", ["not json", "[]", "{}", '{"id": "nope", "observed": {}}',
                                     '{"id": "' + STREAM_ID + '"}'])
def test_malformed_stream_record_is_ignored_rather_than_shown(tmp_path, content):
    path = tmp_path / "record.json"
    path.write_text(content)
    assert q.load_stream_record(path) is None
    assert q.load_stream_record(tmp_path / "missing.json") is None


def test_stream_facts_state_the_recorded_snapshot_without_claiming_it_is_live():
    facts = dict(q.stream_facts(q.load_stream_record()))
    assert facts["Stream ID"] == STREAM_ID
    state = next(value for label, value in facts.items() if label.startswith("State on "))
    assert state.startswith("Terminated. Last reported block 61,175,720.") and "not reached" in state
    assert "0 blocks sent on record" in state and "matched the filter" not in state
    assert "POST /api/streams" in facts["Delivers to"]


def test_stream_facts_do_not_invent_a_gap_when_the_stream_passed_the_burn():
    record = q.load_stream_record() | {"observed": {"status": "Active", "last_block_delivered": 61_200_000,
                                                    "blocks_sent": 1, "errors": 0}}
    state = next(value for label, value in q.stream_facts(record) if label.startswith("State on "))
    assert "not reached" not in state and "Active" in state


def test_walkthrough_explains_why_quicknode_and_shows_the_recorded_stream():
    shown = screen_text(inbox().stream_context())
    assert "Why QuickNode Streams" in shown and "No hash to start from" in shown
    assert STREAM_ID in shown and "Dashboard snapshot" in shown and "Terminated" in shown


def test_the_stream_id_is_findable_in_the_docs_judges_read():
    for doc in ("README.md", "docs/QUICKNODE.md", "docs/DEVPOST.md"):
        assert STREAM_ID in (ROOT / doc).read_text(), doc
    assert "Why QuickNode Streams" in (ROOT / "README.md").read_text()
    assert "## Why this Stream" in (ROOT / "docs" / "QUICKNODE.md").read_text()
