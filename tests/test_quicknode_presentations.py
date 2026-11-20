from types import SimpleNamespace

import pytest

from rebound import quicknode as q
from rebound import ui

WALLET = "0x" + "ab" * 20


def row(**changes):
    return {"key": "live", "discovery_source": "stream", "example": False,
            "stream_signed": True, "stream_received_at": 1_795_000_000,
            "source_verified": True, "block_number": 61175721, "source_confirmations": 101,
            "checked_at": "2026-11-20T10:00:00Z", "attestation_matched": True, "nonce_used": False,
            "status": "ready", "destination_url": None, "source_url": None,
            "tx_hash": "0x" + "cd" * 32, "caller": "0x" + "00" * 20} | changes


def checks(**changes):
    args = {"enabled": True, "delivered": 1, "last_received": 1_795_000_000, "transfers": [row()]} | changes
    return {step.key: step for step in q.walkthrough_checks(**args)}


def screen(control):
    found = [str(control.value)] if isinstance(control, ui.ft.Text) else []
    content = getattr(control, "content", None)
    if isinstance(content, str):
        found.append(content)
    for child in [*(getattr(control, "controls", None) or []), content,
                  getattr(control, "title", None)]:
        if child is not None and not isinstance(child, str):
            found.extend(screen(child))
    return found


def test_hmac_proves_endpoint_authentication_and_never_provider_dashboard_delivery():
    result = checks()
    assert result["authenticated"].state == "verified"
    assert result["authenticated"].title == "Authenticated Stream payload"
    assert result["live"].state == "pending"
    assert "valid HMAC alone" in result["live"].detail
    delivery = q.evidence_steps(row(), "Ready to finish")[1]
    assert delivery.title == "Authenticated Stream payload"
    assert "independent provider dashboard evidence" in delivery.detail


@pytest.mark.parametrize("value", [False, None, 0, 1, "true", "false"])
def test_truthy_nonboolean_metadata_cannot_verify_a_payload_or_chain_state(value):
    assert not q.authenticated_stream_payload(row(stream_signed=value))
    assert q.evidence_steps(row(source_verified=value), "Ready")[0].state == "pending"
    assert q.evidence_steps(row(attestation_matched=value), "Ready")[2].state == "pending"
    if type(value) is not bool:
        assert q.evidence_steps(row(nonce_used=value), "Ready")[2].state == "pending"


@pytest.mark.parametrize("value", [True, False, None, 1.5, "1795000000", -1, 0, 2**256])
def test_bad_delivery_timestamps_are_omitted_without_crashing(value):
    assert q.format_utc(value) == ""
    assert q.evidence_steps(row(stream_received_at=value), "Ready")[1].state == "verified"


def test_source_match_below_threshold_is_observed_without_confirmed_or_multichain_claims():
    steps = q.evidence_steps(row(status="source_confirming", source_confirmations=7,
                                 attestation_matched=False, nonce_used=None), "Source confirming")
    assert steps[0].title == "Source burn matched" and steps[0].state == "observed"
    assert "7 of 12 confirmations" in steps[0].detail
    assert steps[2].state == "pending" and "Arc nonce not read" in steps[2].detail


@pytest.mark.parametrize("status", ["awaiting_attestation", "unavailable", "source_changed"])
def test_incomplete_provider_reads_do_not_pass_transfer_or_mint_checks(status):
    item = row(status=status, attestation_matched=False, nonce_used=None)
    assert q.evidence_steps(item, status)[2].state == "pending"
    assert checks(transfers=[item])["mint"].state == "pending"


def test_unmatched_source_cannot_pass_wallet_discovery():
    assert checks(transfers=[row(source_verified=False, status="unavailable")])["discovery"].state == "pending"


def test_preexisting_verified_mint_is_independent_of_who_delivered_or_sent_it():
    result = checks(transfers=[row(status="completed", nonce_used=True, destination_url="https://arc/tx/example")])
    assert result["mint"].state == "verified"
    assert "does not imply the walkthrough sent" in result["mint"].detail
    assert result["live"].state == "pending"
    assert checks(transfers=[row(status="completed", nonce_used=True)])["mint"].state == "pending"


def test_synthetic_and_adapter_evidence_never_pass_provider_or_chain_milestones():
    example = row(example=True, key="example-stream")
    assert not q.authenticated_stream_payload(example)
    assert all(step.state == "simulated" for step in q.evidence_steps(example, "Ready"))
    assert checks(transfers=[example])["discovery"].state == "pending"
    adapter = checks(delivered=0, local=1, transfers=[row(stream_signed=False)])
    assert all(adapter[key].state == "pending" for key in ("authenticated", "live", "discovery", "mint"))


def test_inspector_keeps_one_compact_delivery_block_and_the_existing_demo_label():
    app = ui.InboxUI(SimpleNamespace(width=390), object())
    shown = " | ".join(screen(app.quicknode_evidence(row())))
    assert "Authenticated Stream payload" in shown
    assert "Burn confirmed" not in shown and "Transfer status verified" not in shown
    assert "Discovered via QuickNode Streams" not in shown
    demo = " | ".join(screen(app.quicknode_evidence(row(example=True, discovery_source="example_stream"))))
    assert "Simulated Stream delivery" in demo
    assert app.quicknode_evidence(row(discovery_source="rpc")) is None


async def test_walkthrough_uses_the_normalized_service_wallet_for_current_search_results():
    class Page:
        width = 390

        def show_dialog(self, dialog):
            self.dialog = dialog

    class Service:
        settings = SimpleNamespace(quicknode_demo_wallet=WALLET.upper().replace("0X", "0x"))

        def stream_status(self, _wallet):
            return {"wallet": WALLET, "enabled": True, "delivered": 1, "last_received": None, "local": 0}

    page = Page()
    app = ui.InboxUI(page, Service())
    app.state.mode, app.state.wallet = "stream", WALLET
    app.state.transfers = [row()]
    await app.quicknode_walkthrough()
    shown = " | ".join(screen(page.dialog))
    assert "1 authenticated indexed burn matched" in shown
    assert "Actual provider delivery" in shown and "Not independently observed here" in shown
