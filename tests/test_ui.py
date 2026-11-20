import asyncio

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
