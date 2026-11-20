import asyncio
from datetime import UTC, datetime

import pytest

from rebound import ui

BURNED_AT = "2026-11-20T06:43:33+00:00"  # Base Sepolia block 61,175,721
MINTED_AT = "2026-11-20T06:45:12+00:00"  # Arc Testnet block 73,147,365
NOW = datetime(2026, 11, 20, 8, 57, 33, tzinfo=UTC)


def live(status="awaiting_attestation", **changes):
    return {"key": "live-case", "tx_hash": "0x" + "aa" * 32, "log_index": 4, "block_number": 61_175_721,
            "amount": "1", "recipient": "0x" + "11" * 20, "status": status, "example": False,
            "source_url": "https://sepolia.basescan.org/tx/0x" + "aa" * 32, "destination_url": None,
            "source_verified": True, "source_confirmations": 101, "burned_at": BURNED_AT,
            "minted_at": None, "checked_at": "2026-11-20T08:57:30+00:00", "discovery_source": "rpc"} | changes


def texts(control) -> list[str]:
    found = [control.value] if isinstance(control, ui.ft.Text) else []
    if isinstance(control, ui.ft.TextButton) and isinstance(control.content, str):
        found.append(control.content)
    for child in [*(getattr(control, "controls", None) or []), getattr(control, "content", None)]:
        if child is not None and not isinstance(child, str):
            found.extend(texts(child))
    return found


@pytest.mark.parametrize("seconds,expected", [
    (-4, "0 s"), (0, "0 s"), (99, "99 s"), (119, "119 s"), (120, "2 min"), (3599, "59 min"),
    (3600, "1 h"), (8040, "2 h 14 min"), (86_400, "1 d"), (93_600, "1 d 2 h"),
])
def test_durations_read_like_a_ledger(seconds, expected):
    assert ui.format_duration(seconds) == expected


def test_clock_shows_the_date_only_for_another_day():
    burned = ui.chain_time(BURNED_AT)
    assert ui.format_clock(burned, NOW) == "06:43:33 UTC"
    assert ui.format_clock(burned, datetime(2026, 11, 21, 1, tzinfo=UTC)) == "2026-11-20 06:43:33 UTC"


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-11-20T06:43:33", 1_763_621_013])
def test_only_service_block_timestamps_are_read(value):
    # A naive or missing value is never completed with the local clock or time zone.
    assert ui.chain_time(value) is None
    assert ui.burn_time(live(burned_at=value), NOW) is None
    assert ui.unfinished_for(live(burned_at=value), NOW) is None


def test_burn_and_mint_times_come_from_block_timestamps():
    item = live("completed", minted_at=MINTED_AT, destination_url="https://explorer.testnet.arc.io/tx/0x" + "bb" * 32)
    assert ui.burn_time(item, NOW) == "Burned 06:43:33 UTC, 2 h 14 min ago"
    assert ui.mint_time(item, NOW) == "Minted 06:45:12 UTC, 99 s after the burn"
    assert ui.mint_time(item | {"burned_at": None}, NOW) == "Minted 06:45:12 UTC"
    lines = texts(ui.InboxUI(object(), object()).proof(item, NOW))
    assert lines.index("Burned 06:43:33 UTC, 2 h 14 min ago") < lines.index("Block 61,175,721, log 4")
    assert "Minted 06:45:12 UTC, 99 s after the burn" in lines
    assert "101 confirmations" in lines


def test_mint_time_needs_the_verified_receipt_link_on_a_live_transfer():
    app = ui.InboxUI(object(), object())
    unlinked = live("completed", minted_at=MINTED_AT)
    assert not any(line.startswith("Minted ") for line in texts(app.proof(unlinked, NOW)))
    example = unlinked | {"example": True, "destination_url": "https://explorer.testnet.arc.io/tx/0x" + "bb" * 32}
    assert not any(line.startswith("Minted ") for line in texts(app.proof(example, NOW)))


@pytest.mark.parametrize("status,expected", [
    ("ready", "Unfinished for 2 h 14 min"), ("awaiting_attestation", "Unfinished for 2 h 14 min"),
    ("source_confirming", "Unfinished for 2 h 14 min"), ("caller_required", "Unfinished for 2 h 14 min"),
    ("completed", None), ("expired", None), ("unavailable", None), ("source_changed", None),
])
def test_stuck_duration_is_shown_only_for_unfinished_transfers(status, expected):
    assert ui.unfinished_for(live(status), NOW) == expected


def test_confirming_burn_shows_progress_toward_the_recovery_threshold():
    app = ui.InboxUI(object(), object())
    assert "7 of 12 Base confirmations" in texts(app.proof(live("source_confirming", source_confirmations=7), NOW))
    assert "1,234 confirmations" in texts(app.proof(live("ready", source_confirmations=1234), NOW))


class LoopPage:
    width = 1440

    def __init__(self):
        self.tasks = []
        self.controls = []
        self.services = []

    def run_task(self, handler, *args):
        # Run the recheck loop for real. The scroll reset needs a client and is covered in test_ui.
        if handler.__name__ == "recheck_loop":
            self.tasks.append(asyncio.ensure_future(handler(*args)))

    def add(self, control):
        self.controls.append(control)

    def update(self):
        pass


class StatusService:
    """Returns the queued statuses, one per recheck, and records request overlap."""

    def __init__(self, *statuses, error=None):
        self.statuses = list(statuses)
        self.error = error
        self.calls = self.active = self.max_active = 0

    async def inspect_transaction(self, tx_hash, *, log_index):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if self.error:
                raise self.error
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"transfers": [live(status, source_confirmations=self.calls)]}
        finally:
            self.active -= 1


def looping_app(monkeypatch, service, on_sleep=None):
    page = LoopPage()
    app = ui.InboxUI(page, service)
    app._installed = True
    monkeypatch.setattr(app, "render", lambda: None)
    clock = {"now": 0.0, "sleeps": 0}

    async def sleep(seconds):
        clock["now"] += seconds
        clock["sleeps"] += 1
        if on_sleep:
            result = on_sleep(app, clock["sleeps"])
            if asyncio.iscoroutine(result):
                await result
        await asyncio.sleep(0)

    app.recheck_sleep = sleep
    app.recheck_clock = lambda: clock["now"]
    return app, page, clock


async def finish(page):
    await asyncio.wait_for(asyncio.gather(*page.tasks), timeout=5)


@pytest.mark.parametrize("item,started", [
    (live("source_confirming"), True), (live("awaiting_attestation"), True),
    (live("ready"), False), (live("completed"), False), (live("expired"), False),
    (live("caller_required"), False), (live("unavailable"), False),
    (live("awaiting_attestation", example=True), False),
    (live("awaiting_attestation", key="example-2"), False),
])
async def test_recheck_starts_only_for_a_waiting_live_transfer(monkeypatch, item, started):
    app, page, _ = looping_app(monkeypatch, StatusService("ready"))
    app.state.transfers = [item]
    app.inspect(item)
    assert bool(page.tasks) is started
    assert app.rechecking(app._recheck) is started
    await finish(page)


async def test_examples_never_recheck(monkeypatch):
    app, page, _ = looping_app(monkeypatch, StatusService("ready"))
    app.state.load_examples()
    waiting = next(item for item in app.state.transfers if item["status"] == "awaiting_attestation")
    app.inspect(waiting)
    assert page.tasks == [] and app._recheck is None


@pytest.mark.parametrize("final", ["ready", "completed", "expired", "unavailable"])
async def test_recheck_follows_the_transfer_until_it_stops_waiting(monkeypatch, final):
    service = StatusService("source_confirming", "awaiting_attestation", final)
    app, page, clock = looping_app(monkeypatch, service)
    item = live("source_confirming", source_confirmations=7)
    app.state.transfers = [item, live("ready", key="other")]
    app.inspect(item)
    await finish(page)
    assert service.calls == 3 and service.max_active == 1
    assert clock["sleeps"] == 3 * ui.RECHECK_SECONDS
    assert app.state.selected["status"] == final
    assert app.state.transfers[0] is app.state.selected  # Updated in place, ledger included.
    assert not app.rechecking(app._recheck) and app._recheck.note == ""


async def test_recheck_stops_after_a_failed_refresh(monkeypatch):
    service = StatusService(error=ValueError("Base Sepolia RPC is unavailable."))
    app, page, _ = looping_app(monkeypatch, service)
    item = live()
    app.state.transfers = [item]
    app.inspect(item)
    await finish(page)
    assert service.calls == 1
    assert app.state.selected is item
    assert "Refresh failed" in app.refresh_error
    assert not app.rechecking(app._recheck)


@pytest.mark.parametrize("action", ["stop", "select", "back", "navigate"])
async def test_recheck_ends_on_stop_selection_or_navigation(monkeypatch, action):
    def interrupt(app, sleeps):
        if sleeps != 3:
            return None
        other = live("completed", key="other")
        return {"stop": app.stop_recheck, "back": app.back,
                "select": lambda: app.inspect(other), "navigate": lambda: app.navigate("/demo")}[action]()

    service = StatusService("awaiting_attestation")
    app, page, clock = looping_app(monkeypatch, service, interrupt)
    app.install()
    item = live()
    app.state.transfers = [item]
    app.inspect(item)
    await finish(page)
    assert service.calls == 0 and clock["sleeps"] == 3
    assert not app.rechecking(app._recheck)


async def test_disconnected_client_skips_rechecks_until_it_reconnects(monkeypatch):
    def connection(app, sleeps):
        if sleeps in (3, 25):
            (app.page.on_disconnect if sleeps == 3 else app.page.on_connect)(None)

    service = StatusService("ready")
    app, page, clock = looping_app(monkeypatch, service, connection)
    app.install()
    item = live()
    app.state.transfers = [item]
    app.inspect(item)
    await finish(page)
    assert service.calls == 1 and clock["sleeps"] == 30
    assert app.state.selected["status"] == "ready"


async def test_recheck_stops_at_the_time_cap(monkeypatch):
    service = StatusService("awaiting_attestation")
    app, page, clock = looping_app(monkeypatch, service)
    item = live()
    app.state.transfers = [item]
    app.inspect(item)
    await finish(page)
    assert clock["now"] == ui.RECHECK_LIMIT_SECONDS
    assert service.calls == ui.RECHECK_LIMIT_SECONDS // ui.RECHECK_SECONDS - 1
    assert service.max_active == 1
    assert app._recheck.note.startswith("Stopped checking after 10 min")
    assert not app.rechecking(app._recheck)


async def test_recheck_never_overlaps_a_manual_refresh(monkeypatch):
    release = asyncio.Event()

    class SlowFirstRefresh(StatusService):
        async def inspect_transaction(self, tx_hash, *, log_index):
            if self.calls == 0:
                self.calls, self.active, self.max_active = 1, 1, 1
                await release.wait()
                self.active = 0
                return {"transfers": [live("awaiting_attestation")]}
            return await super().inspect_transaction(tx_hash, log_index=log_index)

    service = SlowFirstRefresh("ready")
    app, page, clock = looping_app(monkeypatch, service,
                                   lambda _app, sleeps: release.set() if sleeps == 25 else None)
    item = live()
    app.state.transfers = [item]
    app.inspect(item)
    manual = asyncio.create_task(app.refresh_transfer())
    await finish(page)
    await manual
    # Countdowns that ended during the manual request skipped their turn.
    assert service.calls == 2 and service.max_active == 1
    assert clock["sleeps"] == 30
    assert app.state.selected["status"] == "ready"


def test_inspector_shows_countdown_progress_and_stuck_time_in_its_first_frame(monkeypatch):
    page = LoopPage()
    page.run_task = lambda handler, *args: None
    app = ui.InboxUI(page, object())
    app.install()
    item = live("source_confirming", source_confirmations=7)
    app.state.transfers = [item]
    app.inspect(item)
    lines = texts(app.inspector_host.content)
    assert "Checking again in 10 s" in lines and "Stop" in lines
    assert "7 of 12 Base confirmations" in lines
    assert any(line.startswith("Unfinished for ") for line in lines)
    assert any(line.startswith("Burned 06:43:33 UTC") or line.startswith("Burned 2026-11-20 06:43:33 UTC")
               for line in lines)
    app.stop_recheck()
    assert "Stop" not in texts(app.inspector_host.content)
    page.width = 390
    app.render()
    assert any(line.startswith("Unfinished for ") for line in texts(app.inspector_host.content))
