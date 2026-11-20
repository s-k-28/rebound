import asyncio
from types import SimpleNamespace

import pytest

from rebound import ui


class RecordingService:
    def __init__(self):
        self.prepared = False

    async def scan(self, wallet, blocks=2000):
        raise ValueError("Provider unavailable; narrow the range and retry.")

    async def prepare(self, tx_hash, log_index, caller):
        self.prepared = True
        return {"transaction": {"to": "0x" + "11" * 20}, "recipient": caller}


def completed_without_receipt():
    return {"key": "source-case", "tx_hash": "0x" + "aa" * 32, "log_index": 4,
            "status": "completed", "example": False, "destination_url": None,
            "discovery_source": "stream"}


async def test_submitted_arc_receipt_is_verified_without_replacing_indexed_receipt(monkeypatch):
    source = completed_without_receipt()
    destination_hash = "0x" + "bb" * 32
    class ReceiptService:
        async def inspect_transaction(self, tx_hash, *, log_index, destination_hash):
            assert (tx_hash, log_index, destination_hash) == (source["tx_hash"], 4, "0x" + "bb" * 32)
            return {"transfers": [source | {"submitted_destination_hash": destination_hash,
                "submitted_receipt_verified": True, "submitted_receipt_url": "https://explorer.testnet.arc.io/tx/" + destination_hash}]}
    app = ui.InboxUI(object(), ReceiptService())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.transfers = [source]
    app.state.selected = source
    app.receipt_hash_input.value = destination_hash
    await app.verify_arc_receipt()
    assert app.state.selected["submitted_receipt_verified"] is True
    assert app.state.selected["destination_url"] is None
    assert app.state.selected["discovery_source"] == "stream"
    assert app.state.transfers == [app.state.selected]
    assert app.receipt_error == ""


async def test_failed_receipt_match_does_not_create_a_link(monkeypatch):
    source = completed_without_receipt()
    class ReceiptService:
        async def inspect_transaction(self, tx_hash, *, log_index, destination_hash):
            return {"transfers": [source | {"submitted_destination_hash": destination_hash,
                "submitted_receipt_verified": False, "submitted_receipt_url": None}]}
    app = ui.InboxUI(object(), ReceiptService())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.selected = source
    app.receipt_hash_input.value = "0x" + "bb" * 32
    await app.verify_arc_receipt()
    assert app.state.selected["submitted_receipt_url"] is None
    assert "not verified" in app.receipt_error.lower()


async def test_receipt_verification_cannot_replace_a_new_selection(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    source = completed_without_receipt()
    class DelayedReceipt:
        async def inspect_transaction(self, tx_hash, *, log_index, destination_hash):
            started.set()
            await finish.wait()
            return {"transfers": [source | {"submitted_receipt_verified": True,
                "submitted_receipt_url": "https://explorer.testnet.arc.io/tx/" + destination_hash}]}
    app = ui.InboxUI(object(), DelayedReceipt())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.selected = source
    app.receipt_hash_input.value = "0x" + "bb" * 32
    pending = asyncio.create_task(app.verify_arc_receipt())
    await started.wait()
    other = source | {"key": "other-case", "tx_hash": "0x" + "cc" * 32}
    app.inspect(other)
    finish.set()
    await pending
    assert app.state.selected is other
    assert app.receipt_hash_input.value == ""
    assert app.receipt_error == ""


async def test_editing_receipt_hash_invalidates_a_pending_result(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    source = completed_without_receipt()
    class DelayedReceipt:
        async def inspect_transaction(self, tx_hash, *, log_index, destination_hash):
            started.set()
            await finish.wait()
            return {"transfers": [source | {"submitted_receipt_verified": True,
                "submitted_receipt_url": "https://explorer.testnet.arc.io/tx/" + destination_hash}]}
    app = ui.InboxUI(object(), DelayedReceipt())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.selected = source
    app.receipt_hash_input.value = "0x" + "bb" * 32
    pending = asyncio.create_task(app.verify_arc_receipt())
    await started.wait()
    app.receipt_hash_input.value = "0x" + "dd" * 32
    finish.set()
    await pending
    assert app.state.selected is source
    assert not app.state.selected.get("submitted_receipt_url")


@pytest.mark.parametrize("changes", [{"example": True}, {"status": "ready"},
                                     {"destination_url": "https://explorer.testnet.arc.io/tx/indexed"}])
async def test_receipt_form_is_only_for_unlinked_completed_live_transfers(monkeypatch, changes):
    class ForbiddenService:
        async def inspect_transaction(self, *_args, **_kwargs):
            raise AssertionError("No request should be made for this selection")
    app = ui.InboxUI(object(), ForbiddenService())
    monkeypatch.setattr(app, "render", lambda: None)
    source = completed_without_receipt() | changes
    app.state.selected = source
    app.receipt_hash_input.value = "0x" + "bb" * 32
    await app.verify_arc_receipt()
    assert app.state.selected is source


async def test_invalid_submitted_hash_stays_a_form_error(monkeypatch):
    app = ui.InboxUI(object(), object())
    monkeypatch.setattr(app, "render", lambda: None)
    source = completed_without_receipt()
    app.state.selected = source
    app.receipt_hash_input.value = "not-a-hash"
    await app.verify_arc_receipt()
    assert "32-byte" in app.receipt_error
    assert app.state.selected is source


def test_unverified_submitted_url_cannot_be_displayed_as_a_receipt():
    app = ui.InboxUI(object(), object())
    item = completed_without_receipt() | {"submitted_receipt_verified": False,
        "submitted_receipt_url": "https://explorer.testnet.arc.io/tx/" + "0x" + "bb" * 32}
    assert app.submitted_receipt_url(item) is None
    assert app.receipt_editor_available(item)


async def test_refresh_preserves_separately_verified_submitted_receipt(monkeypatch):
    destination_hash = "0x" + "bb" * 32
    source = completed_without_receipt() | {"submitted_destination_hash": destination_hash,
        "submitted_receipt_verified": True,
        "submitted_receipt_url": "https://explorer.testnet.arc.io/tx/" + destination_hash}
    class ReceiptService:
        async def inspect_transaction(self, tx_hash, *, log_index, destination_hash):
            assert destination_hash == "0x" + "bb" * 32
            return {"transfers": [source]}
    app = ui.InboxUI(object(), ReceiptService())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.selected = source
    app.state.transfers = [source]
    await app.refresh_transfer()
    assert app.state.selected["submitted_receipt_verified"] is True
    assert app.state.selected["destination_url"] is None


async def test_pending_review_is_discarded_after_switching_transfers(monkeypatch):
    started = asyncio.Event()
    finished = asyncio.Event()

    class DelayedService:
        async def prepare(self, tx_hash, log_index, caller):
            started.set()
            await finished.wait()
            return {"transaction": {"from": caller}, "tx_hash": tx_hash, "log_index": log_index}

    app = ui.InboxUI(object(), DelayedService())
    monkeypatch.setattr(app, "render", lambda: None)
    first = {"key": "original", "tx_hash": "0x" + "aa" * 32, "log_index": 2,
             "example": False, "status": "ready"}
    second = {**first, "key": "different", "tx_hash": "0x" + "bb" * 32}
    app.inspect(first)
    app.caller_input.value = "0x" + "11" * 20
    preparing = asyncio.create_task(app.prepare())
    await started.wait()
    app.inspect(second)
    finished.set()
    await preparing
    assert app.state.selected is second
    assert app.state.prepared is None
    assert not app.preparing


async def test_refresh_keeps_selected_transfer_visible_after_completion(monkeypatch):
    original = {"key": "one", "tx_hash": "0x" + "aa" * 32, "log_index": 4,
                "status": "ready", "discovery_source": "stream"}
    other = {"key": "two", "status": "awaiting_attestation"}

    class RefreshService:
        async def inspect_transaction(self, tx_hash, *, log_index):
            assert (tx_hash, log_index) == (original["tx_hash"], 4)
            return {"transfers": [original | {"status": "completed", "discovery_source": "receipt"}]}

    app = ui.InboxUI(object(), RefreshService())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.transfers = [original, other]
    app.state.selected = original
    app.state.filter = "attention"
    app.state.prepared = {"stale": True}
    await app.refresh_transfer()
    assert app.state.selected == original | {"status": "completed"}
    assert app.state.visible_transfers() == [app.state.selected, other]
    assert app.state.prepared is None


async def test_shared_case_rechecks_one_public_source_event(monkeypatch):
    tx_hash = "0x" + "aa" * 32
    item = {"key": "case", "tx_hash": tx_hash, "log_index": 4, "status": "completed"}

    class Page:
        url = "wss://rebound.example/?wallet=unrelated"

    class CaseService:
        calls = []

        async def inspect_transaction(self, source_hash, *, log_index):
            self.calls.append((source_hash, log_index))
            return {"transfers": [item], "coverage": "One source receipt"}

    service = CaseService()
    app = ui.InboxUI(Page(), service)
    monkeypatch.setattr(app, "render", lambda: None)
    await app.open_case("/transfer/malformed/4")
    assert service.calls == []
    assert "case link is incomplete" in app.state.error
    await app.open_case(f"/transfer/{tx_hash}/4")
    assert service.calls == [(tx_hash, 4)]
    assert app.state.selected is item
    assert app.case_link(item) == f"https://rebound.example/transfer/{tx_hash}/4"
    assert app.case_link(item | {"example": True}) is None


async def test_live_sample_scans_the_controlled_recovery_wallet_and_fixed_window(monkeypatch):
    tx_hash = "0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23"
    item = {"key": "controlled-recovery", "tx_hash": tx_hash, "log_index": 4, "status": "completed"}
    class ControlledSample:
        async def scan(self, wallet, *, blocks, end_block):
            assert wallet == "0x395809d8da4a70145d5060448070a15367df722c"
            assert (blocks, end_block) == (200, 61175721)
            return {"transfers": [item], "coverage": "Controlled recovery source window"}
    app = ui.InboxUI(object(), ControlledSample())
    monkeypatch.setattr(app, "render", lambda: None)
    await app.live_sample()
    assert app.state.example is False
    assert app.state.transfers == [item]


async def test_route_change_replaces_selected_demo_with_the_requested_live_case(monkeypatch):
    tx_hash = "0x" + "aa" * 32
    item = {"key": "live-case", "tx_hash": tx_hash, "log_index": 4, "status": "completed"}
    class CaseService:
        async def inspect_transaction(self, source_hash, *, log_index):
            assert (source_hash, log_index) == (tx_hash, 4)
            return {"transfers": [item], "coverage": "One source receipt"}
    app = ui.InboxUI(object(), CaseService())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.load_examples()
    app.state.selected = app.state.transfers[0]
    await app.route_changed(SimpleNamespace(route=f"/transfer/{tx_hash}/4"))
    assert app.state.example is False
    assert app.state.selected is item


async def test_navigation_to_demo_does_not_accept_a_previous_case_result(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    tx_hash = "0x" + "aa" * 32
    item = {"key": "old-case", "tx_hash": tx_hash, "log_index": 4, "status": "completed"}
    class DelayedCase:
        async def inspect_transaction(self, source_hash, *, log_index):
            started.set()
            await finish.wait()
            return {"transfers": [item], "coverage": "One source receipt"}
    app = ui.InboxUI(object(), DelayedCase())
    monkeypatch.setattr(app, "render", lambda: None)
    pending = asyncio.create_task(app.route_changed(SimpleNamespace(route=f"/transfer/{tx_hash}/4")))
    await started.wait()
    await app.route_changed(SimpleNamespace(route="/demo"))
    finish.set()
    await pending
    assert app.state.example is True
    assert len(app.state.transfers) == 4
    assert app.state.selected is None


async def test_synthetic_wallet_has_a_separate_stream_flow_without_live_requests():
    from rebound.examples import EXAMPLE_WALLET

    state = ui.InboxState()
    await state.scan(object(), EXAMPLE_WALLET, 2000)
    assert state.example and len(state.transfers) == 4
    await state.scan(object(), EXAMPLE_WALLET, 2000, mode="stream")
    item, = state.transfers
    assert item["recipient"] == EXAMPLE_WALLET
    assert item["discovery_source"] == "example_stream"
    assert item["example"] and item["destination_url"] is None
    with pytest.raises(ValueError, match="only available for verified live transfers"):
        await state.prepare(object(), item, EXAMPLE_WALLET)


def test_attention_filter_excludes_completed_but_preserves_waiting():
    state = ui.InboxState()
    state.load_examples()
    state.filter = "attention"
    assert {x["status"] for x in state.visible_transfers()} == {
        "ready", "awaiting_attestation", "caller_required"
    }
    state.filter = "completed"
    assert [x["status"] for x in state.visible_transfers()] == ["completed"]


def test_search_filters_existing_results_without_inventing_totals():
    state = ui.InboxState()
    state.load_examples()
    state.query = "128.5"
    assert [item["amount"] for item in state.visible_transfers()] == ["128.5"]
    assert state.counts() == {"all": 4, "attention": 3, "ready": 1, "completed": 1}
    state.query = "nothing matches"
    assert state.visible_transfers() == []


def test_search_does_not_rebuild_the_focused_field_and_clears_hidden_selection(monkeypatch):
    class Page:
        width = 1440

    app = ui.InboxUI(Page(), RecordingService())
    app.state.load_examples()
    app.state.selected = app.state.transfers[0]
    app.state.prepared = {"transaction": "stale"}
    # Only the actual browser update is replaced; controller/filter behavior stays real.
    monkeypatch.setattr(app.ledger_host, "update", lambda: None)
    monkeypatch.setattr(app.inspector_host, "update", lambda: None)
    def forbidden_full_render():
        raise AssertionError("Typing must not reparent the focused search field")
    monkeypatch.setattr(app, "render", forbidden_full_render)
    app.search_input.value = "4"
    app.search()
    app.search_input.value = "42"
    app.search()
    assert [item["amount"] for item in app.state.visible_transfers()] == ["42"]
    assert app.state.selected is None
    assert app.state.prepared is None


def test_filter_change_drops_evidence_for_a_hidden_transfer(monkeypatch):
    class Page:
        width = 1440

    app = ui.InboxUI(Page(), RecordingService())
    app.state.load_examples()
    app.state.selected = app.state.transfers[0]
    monkeypatch.setattr(app, "render", lambda: None)
    app.set_filter("completed")
    assert app.state.selected is None


@pytest.mark.parametrize("width,expected_scroll", [(390, "workspace"), (900, "workspace"), (1440, "inspector")])
async def test_selecting_transfer_starts_at_the_top_of_its_details(monkeypatch, width, expected_scroll):
    class Page:
        pass

    page = Page()
    page.width = width
    app = ui.InboxUI(page, RecordingService())
    item = {"key": "selected-transfer"}
    app.state.selected = item
    scrolled = []

    async def workspace_scroll(**kwargs):
        scrolled.append(("workspace", kwargs))

    async def inspector_scroll(**kwargs):
        scrolled.append(("inspector", kwargs))

    monkeypatch.setattr(app.workspace_view, "scroll_to", workspace_scroll)
    monkeypatch.setattr(app.inspector_scroll, "scroll_to", inspector_scroll)
    await app.finish_inspector_reveal(item)
    assert scrolled == [(expected_scroll, {"offset": 0, "duration": 0})]


async def test_obsolete_selection_does_not_scroll_the_current_view(monkeypatch):
    class Page:
        width = 390

    app = ui.InboxUI(Page(), RecordingService())
    app.state.selected = {"key": "new-transfer"}
    async def forbidden_scroll(**_kwargs):
        raise AssertionError("An old reveal must not scroll the user's new selection")
    monkeypatch.setattr(app.workspace_view, "scroll_to", forbidden_scroll)
    await app.finish_inspector_reveal({"key": "old-transfer"})


@pytest.mark.parametrize("motion_enabled", [False, True])
async def test_initial_deep_link_does_not_require_a_client_scroll_listener(monkeypatch, motion_enabled):
    tx_hash = "0x" + "aa" * 32
    item = {"key": "initial-case", "tx_hash": tx_hash, "log_index": 4, "status": "completed"}
    pending = []
    class Page:
        width = 390
        def run_task(self, handler, *args):
            pending.append((handler, args))
    class CaseService:
        async def inspect_transaction(self, source_hash, *, log_index):
            return {"transfers": [item], "coverage": "One source receipt"}
    app = ui.InboxUI(Page(), CaseService())
    app._installed = True
    app.motion_enabled = motion_enabled
    monkeypatch.setattr(app, "render", lambda: None)
    monkeypatch.setattr(app.inspector_host, "update", lambda: None)
    async def not_mounted(**_kwargs):
        raise RuntimeError("Timeout waiting for invoke method listener for Column(22).scroll_to")
    monkeypatch.setattr(app.workspace_view, "scroll_to", not_mounted)
    await app.navigate(f"/transfer/{tx_hash}/4")
    for handler, args in pending:
        await handler(*args)
    assert app.state.selected is item


async def test_new_case_after_demo_still_resets_existing_mobile_scroll(monkeypatch):
    tx_hash = "0x" + "aa" * 32
    item = {"key": "next-case", "tx_hash": tx_hash, "log_index": 4, "status": "completed"}
    pending, scrolled = [], []
    class Page:
        width = 390
        def run_task(self, handler, *args):
            pending.append((handler, args))
    class CaseService:
        async def inspect_transaction(self, source_hash, *, log_index):
            return {"transfers": [item], "coverage": "One source receipt"}
    app = ui.InboxUI(Page(), CaseService())
    app._installed = True
    monkeypatch.setattr(app, "render", lambda: None)
    async def reset_scroll(**kwargs):
        scrolled.append(kwargs)
    monkeypatch.setattr(app.workspace_view, "scroll_to", reset_scroll)
    await app.navigate("/demo")
    await app.navigate(f"/transfer/{tx_hash}/4")
    for handler, args in pending:
        await handler(*args)
    assert scrolled == [{"offset": 0, "duration": 0}]


@pytest.mark.parametrize("error", [
    RuntimeError("Session closed"),
    RuntimeError("Timeout waiting for invoke method listener for Column(22).scroll_to"),
    RuntimeError("TimeoutException after 0:00:10.000000: Timeout waiting for invoke method listener for Column(22).scroll_to"),
    TimeoutError("Timeout waiting for invokeMethod scroll_to call"),
])
async def test_visual_scroll_tolerates_known_client_lifecycle_failures(monkeypatch, error):
    class Page:
        width = 390
    app = ui.InboxUI(Page(), RecordingService())
    item = {"key": "selected"}
    app.state.selected = item
    async def disconnected(**_kwargs):
        raise error
    monkeypatch.setattr(app.workspace_view, "scroll_to", disconnected)
    await app.finish_inspector_reveal(item)
    assert app.state.selected is item


async def test_visual_scroll_does_not_hide_unrelated_runtime_failures(monkeypatch):
    class Page:
        width = 390
    app = ui.InboxUI(Page(), RecordingService())
    item = {"key": "selected"}
    app.state.selected = item
    async def broken_scroll(**_kwargs):
        raise RuntimeError("Unexpected invalid scroll state")
    monkeypatch.setattr(app.workspace_view, "scroll_to", broken_scroll)
    with pytest.raises(RuntimeError, match="Unexpected invalid scroll state"):
        await app.finish_inspector_reveal(item)


async def test_example_cannot_prepare_even_if_action_is_called_directly():
    state = ui.InboxState()
    state.load_examples()
    service = RecordingService()
    with pytest.raises(ValueError, match="example"):
        await state.prepare(service, state.transfers[0], "0x" + "11" * 20)
    assert service.prepared is False


async def test_failed_live_scan_cannot_leave_example_data_presented_as_live():
    state = ui.InboxState()
    state.load_examples()
    await state.scan(RecordingService(), "0x" + "11" * 20, 2000)
    assert state.example is False
    assert state.transfers == []
    assert state.error == "Provider unavailable; narrow the range and retry."
    assert state.busy is False


async def test_completed_transfer_cannot_prepare():
    state = ui.InboxState()
    service = RecordingService()
    transfer = {"status": "completed", "example": False}
    with pytest.raises(ValueError, match="ready"):
        await state.prepare(service, transfer, "0x" + "11" * 20)
    assert service.prepared is False


def test_detail_of_restricted_transfer_explains_caller_not_recipient():
    detail = ui.state_copy("caller_required")
    assert "caller" in detail[1].lower()
    assert "refund" not in detail[0].lower()


def test_page_can_render_each_example_detail_and_narrow_layout():
    # Rendering every branch catches invalid control arguments against installed Flet.
    class Page:
        width = 1200

        def __init__(self):
            self.controls = []
            self.services = []

        def add(self, control):
            self.controls.append(control)

        def update(self):
            pass

        def run_task(self, _handler, *_args):
            pass

    page = Page()
    app = ui.InboxUI(page, RecordingService())
    app.install()
    app.examples()
    for transfer in app.state.transfers:
        app.inspect(transfer)
    page.width = 390
    app.back()
    assert app.narrow is True
    assert app.state.prepared is None
    assert len(page.controls) == 1
