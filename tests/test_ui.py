import asyncio
from types import SimpleNamespace

import pytest

from rebound import protocol, ui


async def test_history_end_time_shows_the_services_safe_validation_error():
    class Validator:
        async def scan(self, _wallet, *, blocks, end_time):
            assert end_time == "invalid time"
            raise ValueError("Enter a valid ending UTC date and time.")
    state = ui.InboxState()
    await state.scan(Validator(), "0x" + "11" * 20, 200, end_time=" invalid time ")
    assert state.error == "Enter a valid ending UTC date and time."
    assert not state.busy


async def test_history_choice_passes_only_the_active_nonempty_anchor(monkeypatch):
    class Scanner:
        def __init__(self):
            self.calls = []

        async def scan(self, wallet, *, blocks, **options):
            self.calls.append((wallet, blocks, options))
            return {"transfers": [], "coverage": "Fixture fixed window"}
    service = Scanner()
    app = ui.InboxUI(object(), service)
    monkeypatch.setattr(app, "render", lambda: None)
    app.wallet_input.value = "0x" + "11" * 20
    app.end_block.value = "61175721"
    app.end_time.value = " 2026-11-20 06:44 "
    for choice in ("latest", "time", "block"):
        app.history_ending.value = choice
        await app.scan()
    app.history_ending.value = "time"
    app.end_time.value = " "
    await app.scan()
    assert [options for _, _, options in service.calls] == [
        {}, {"end_time": "2026-11-20 06:44"}, {"end_block": 61175721}, {}]


@pytest.mark.parametrize("width", [1440, 390])
def test_history_window_mounts_only_the_active_input_and_fits_both_layouts(width):
    app = ui.InboxUI(SimpleNamespace(width=width), object())
    for choice, active in [("latest", None), ("block", app.end_block), ("time", app.end_time)]:
        app.history_ending.value = choice
        panel = app.history_window()
        fields = panel.controls[0]
        assert fields.controls == [app.history_ending] + ([active] if active else [])
        if width == 390:
            assert isinstance(fields, ui.ft.Column)
            assert all(control.width is None for control in fields.controls)


def test_related_amounts_preserve_large_uint256_fractional_units():
    amount = protocol.usdc(10**60 + 123_457)
    assert ui.aligned_amounts(amount, "0.000001", "0.123456") == [
        f"{10**54:,}.123457", "0.000001", "0.123456"]


def test_example_proof_is_labeled_inside_the_proof_section():
    app = ui.InboxUI(object(), object())
    example = ui.example_transfers()[0]
    proof = app.proof(example)
    assert "SIMULATED PROOF" in proof.controls[0].content.value
    live = app.proof(example | {"example": False, "source_verified": True})
    assert all(not isinstance(control, ui.ft.Container) for control in live.controls)


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


@pytest.mark.parametrize("mode,search,end_block,expected", [
    ("wallet", "", "", "Enter a wallet address."),
    ("wallet", "0x1234", "", "Enter the full wallet address: 0x followed by 40 hex characters."),
    ("wallet", "0x" + "11" * 20, "", ""),
    ("wallet", " 0x" + "AB" * 20 + " ", "", ""),
    ("wallet", "0x" + "11" * 20, "61,175,721", "Use digits only for the end block."),
    ("wallet", "0x" + "11" * 20, "٦١", "Use digits only for the end block."),
    ("stream", "0x" + "11" * 20, "abc", ""),
    ("transaction", "", "", "Paste a Base Sepolia transaction hash."),
    ("transaction", "0xabc", "", "Enter the full transaction hash: 0x followed by 64 hex characters."),
    ("transaction", "0x" + "aa" * 32, "", ""),
])
def test_lookup_error_matches_the_service_validators(mode, search, end_block, expected):
    assert ui.lookup_error(mode, search, end_block) == expected
    if not expected and mode == "transaction":
        assert protocol.hash32(search.strip())
    elif not expected:
        assert protocol.address(search.strip())


def test_scan_status_names_the_exact_range_or_target():
    wallet = "0x395809d8da4a70145d5060448070a15367df722c"
    assert ui.block_window(200, "61175721") == (61_175_522, 61_175_721)
    assert ui.block_window(2000, "") is None
    assert ui.block_window(2000, "100") == (0, 100)
    assert ui.scan_status_line("wallet", wallet, 200, "61175721") == (
        "Scanning Base Sepolia blocks 61,175,522–61,175,721 for 0x3958…722c")
    assert ui.scan_status_line("wallet", wallet, 2000) == "Scanning the latest 2,000 Base Sepolia blocks for 0x3958…722c"
    assert ui.scan_status_line("transaction", "0x9064" + "0" * 56 + "50b5", 2000) == (
        "Checking Base Sepolia transaction 0x9064…50b5")
    assert ui.scan_status_line("stream", wallet, 2000) == "Searching the Stream index for 0x3958…722c"


def test_empty_result_names_what_was_searched_and_offers_a_wider_window():
    assert ui.empty_result_message("wallet", 200, "61175721") == (
        "No supported transfers in Base Sepolia blocks 61,175,522–61,175,721.")
    assert ui.empty_result_message("wallet", 2000) == "No supported transfers in the latest 2,000 Base Sepolia blocks."
    assert "original Base Sepolia burn" in ui.empty_result_message("transaction", 2000)
    assert "Stream index" in ui.empty_result_message("stream", 2000)
    assert [ui.wider_window(size) for size in ui.HISTORY_WINDOWS] == [2000, 10_000, 50_000, None]
    assert ui.filter_empty_message("completed", "") == "No completed transfers in this lookup."
    assert ui.filter_empty_message("all", "  zzz ") == "No transfers in this view match “zzz”."
    assert ui.filter_empty_message("all", "x" * 40).endswith("…”.")


async def test_invalid_input_stays_at_the_field_and_keeps_current_results(monkeypatch):
    class NoNetwork:
        async def scan(self, *_args, **_kwargs):
            raise AssertionError("an invalid address must not reach the service")

        async def inspect_transaction(self, *_args, **_kwargs):
            raise AssertionError("an invalid hash must not reach the service")

    app = ui.InboxUI(object(), NoNetwork())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.load_examples()
    app.wallet_input.value = "0x1234"
    await app.scan()
    assert app.input_error.startswith("Enter the full wallet address")
    assert app.input_error_field is app.wallet_input
    assert app.state.example and len(app.state.transfers) == 4 and not app.state.busy
    app.sync_input_error()
    assert app.input_error_text.visible and app.wallet_input.border_color == ui.RED
    app.input_changed()
    assert app.input_error == "" and not app.input_error_text.visible
    app.wallet_input.value = "0x1234"
    await app.scan()
    # A handler that replaces the value (here: the examples button) takes the stale message with it.
    app.wallet_input.value = ui.EXAMPLE_WALLET
    app.sync_input_error()
    assert not app.input_error_text.visible
    app.show_transaction = True
    app.tx_input.value = "0xabc"
    await app.inspect_transaction()
    assert app.input_error_field is app.tx_input and app.state.example


async def test_loading_frame_never_shows_the_previous_lookup(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    class SlowScan:
        async def scan(self, wallet, *, blocks, end_block):
            started.set()
            await finish.wait()
            return {"transfers": [], "coverage": "Base Sepolia blocks 61,175,522–61,175,721 (fixed range)."}

    app = ui.InboxUI(SimpleNamespace(width=1440), SlowScan())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.load_examples()
    app.state.error = "stale"
    pending = asyncio.create_task(app.live_sample())
    await started.wait()
    assert app.state.busy and not app.has_records
    loading = app.ledger_content()
    assert len(loading.controls) == 1
    lines = [control.value for control in loading.controls[0].content.controls]
    assert lines[0] == "Scanning Base Sepolia blocks 61,175,522–61,175,721 for 0x3958…722c"
    finish.set()
    await pending
    # A zero-result lookup shows no tabs, no zero counts and no filter.
    assert app.state.scanned and app.state.transfers == [] and not app.has_records
    assert app.inbox().content.controls[0] is app.ledger_host


async def test_retry_and_widen_rerun_the_last_lookup(monkeypatch):
    calls = []

    class Recording:
        async def scan(self, wallet, *, blocks, **options):
            calls.append((wallet, blocks, options))
            if len(calls) == 1:
                raise ui.UpstreamError("Base Sepolia RPC is unavailable. Retry or configure another RPC.")
            return {"transfers": [], "coverage": "", "from_block": 1, "to_block": blocks}

    app = ui.InboxUI(object(), Recording())
    monkeypatch.setattr(app, "render", lambda: None)
    app.wallet_input.value = "0x" + "11" * 20
    await app.scan()
    assert app.state.error.startswith("Base Sepolia RPC is unavailable")
    await app.retry()
    assert app.state.error == "" and app.state.scanned
    await app.widen()
    assert [blocks for _, blocks, _ in calls] == [2000, 2000, 10_000]
    assert app.history.value == "10000" and app.last_lookup["blocks"] == 10_000


async def test_retry_and_widen_keep_the_submitted_utc_window(monkeypatch):
    calls = []

    class Recording:
        async def scan(self, wallet, *, blocks, **options):
            calls.append((wallet, blocks, options))
            return {"transfers": [], "coverage": "Resolved UTC window"}

    wallet = "0x" + "11" * 20
    anchor = "2026-11-20 06:44"
    app = ui.InboxUI(object(), Recording())
    monkeypatch.setattr(app, "render", lambda: None)
    app.wallet_input.value = wallet
    app.history_ending.value = "time"
    app.end_time.value = anchor
    await app.scan()
    assert app.lookup_line() == "Resolving the UTC cutoff for 0x1111…1111"

    app.wallet_input.value = "0x" + "22" * 20
    app.history_ending.value = "block"
    app.end_block.value = "42"
    await app.retry()
    app.end_time.value = "2026-11-21 00:00"
    await app.widen()
    assert calls == [(wallet, blocks, {"end_time": anchor}) for blocks in (2000, 2000, 10_000)]


async def test_invalid_hidden_block_opens_history_options(monkeypatch):
    app = ui.InboxUI(object(), RecordingService())
    monkeypatch.setattr(app, "render", lambda: None)
    app.wallet_input.value = "0x" + "11" * 20
    app.history_ending.value = "block"
    app.end_block.value = "bad-block"

    await app.scan()
    assert app.show_history and app.input_error_field is app.end_block
    assert app.input_error_text.visible and not app.state.error
    assert app.input_error_notice.visible and app.input_error_notice.live_region


def test_clear_filter_restores_every_transfer(monkeypatch):
    app = ui.InboxUI(object(), RecordingService())
    monkeypatch.setattr(app, "render", lambda: None)
    app.state.load_examples()
    app.state.filter, app.state.query = "completed", "zzz"
    assert app.state.visible_transfers() == []
    app.clear_filter()
    assert len(app.state.visible_transfers()) == 4 and app.search_input.value == ""


def test_snackbars_share_one_style_and_phone_controls_meet_tap_size():
    class Page:
        width = 390

        def __init__(self):
            self.dialogs = []

        def show_dialog(self, dialog):
            self.dialogs.append(dialog)

    page = Page()
    app = ui.InboxUI(page, RecordingService())
    app.notify("Evidence copied")
    snack, = page.dialogs
    assert snack.bgcolor == ui.INK and snack.behavior == ui.ft.SnackBarBehavior.FLOATING
    assert snack.content.value == "Evidence copied"
    assert app.link("History options").height == 44
    assert app.button("Refresh this transfer").height == 44
    page.width = 1440
    assert app.link("History options").height == 30
    assert app.button("Refresh this transfer").height == 40


def test_phone_sheet_pins_the_back_bar_above_the_scrolling_details():
    class Page:
        width = 390

        def __init__(self):
            self.controls, self.services = [], []

        def add(self, control):
            self.controls.append(control)

        def update(self):
            pass

        def run_task(self, _handler, *_args):
            pass

    app = ui.InboxUI(Page(), RecordingService())
    app.install()
    app.examples()
    app.inspect(app.state.transfers[0])
    sheet = app.body_host.content
    back, scroll = sheet.controls
    assert scroll is app.workspace_view and app.inspector_host in scroll.controls
    assert back.content.controls[0].content == "All transfers"
    app.back()
    assert app.body_host.content is app.workspace_view
