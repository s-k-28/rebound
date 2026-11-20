"""Flet review and status panel for finishing one ready transfer from a browser wallet.

Everything the user reads or approves is rendered here in Python. The wallet tab
(rebound/wallet.py) only connects the wallet and passes it the stored transaction.
"""
import asyncio
import time
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import flet as ft

from rebound import ui as inbox_ui
from rebound.protocol import ARC_EXPLORER, hash32
from rebound.ui import display_amount
from rebound.wallet_session import FINAL, SessionConflict, SessionNotFound, sessions_for


def _token(*names: str, default: str) -> str:
    """Use the inbox's palette names (ledger tokens first) so this module tracks ui.py."""
    return next((getattr(inbox_ui, name) for name in names if hasattr(inbox_ui, name)), default)


INK = _token("INK", default="#141A17")
INK_2 = _token("INK_2", "MUTED", default="#48514C")
INK_3 = _token("INK_3", "MUTED", default="#68706B")
ACCENT = _token("ACCENT", "BLUE", default="#1D5A44")
AMBER = _token("AMBER", default="#9A5B0B")
RED = _token("RED", default="#A8332B")
RULE = _token("RULE", "LINE", default="#E1E4E0")
MONO = _token("MONO", default="PlexMono")

FAUCET = "https://faucet.circle.com/"
POLL_SECONDS = 1.5
POLL_LIMIT_SECONDS = 15 * 60
SENDING_PATIENCE_SECONDS = 180
STEPS = ("Connect", "Review", "Send", "Verify")
STEP_OF = {"created": 0, "connected": 1, "reviewed": 1, "approving": 2, "approved": 2, "sending": 2,
           "submitted": 3, "unknown": 3, "unrelated": 3, "confirmed": 4, "failed": 3}


def adapter_link(page, session_id: str) -> str | None:
    page_url = urlsplit(getattr(page, "url", "") or "")
    scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(page_url.scheme)
    if scheme is None or not page_url.netloc:
        return None
    return urlunsplit((scheme, page_url.netloc, f"{page_url.path.rstrip('/')}/wallet/adapter/{session_id}", "", ""))


def wallet_available(inbox, item: dict) -> bool:
    """Offered only for live, verified ready transfers on a server that can verify receipts."""
    return (item.get("status") == "ready" and not inbox.state.example and not item.get("example")
            and not str(item.get("key", "")).startswith("example-")
            and all(callable(getattr(inbox.service, name, None)) for name in ("prepare", "inspect_transaction"))
            and adapter_link(inbox.page, "x") is not None)


def wallet_button(inbox, item: dict):
    if not wallet_available(inbox, item):
        return None
    return inbox.button("Finish with browser wallet", lambda _: WalletPanel(inbox, item).open(),
                        primary=True, icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED)


def gwei(wei: int) -> str:
    return f"{Decimal(wei) / Decimal(10**9):f}".rstrip("0").rstrip(".") + " gwei"


class WalletPanel:
    def __init__(self, inbox, item: dict):
        self.inbox = inbox
        self.page = inbox.page
        self.item = item
        self.sessions = sessions_for(inbox.service)
        self.session = None
        self.adapter_url = None
        self.dialog = None
        self.is_open = False
        self.opened_tab = False
        self.busy = False
        self.expired = False
        self.timed_out = False
        self.local_error = ""
        self.seen = -1
        self.sending_since = None
        field = inbox_ui.field_style() if callable(getattr(inbox_ui, "field_style", None)) else {
            "border_color": RULE, "focused_border_color": ACCENT, "border_radius": 6, "autocorrect": False}
        self.hash_input = ft.TextField(hint_text="Transaction hash from your wallet's activity, 0x…", **field)

    # Lifecycle -----------------------------------------------------------------

    def open(self) -> None:
        try:
            self.session = self.sessions.create(self.item["tx_hash"], self.item["log_index"])
        except (SessionConflict, ValueError) as exc:
            self.page.show_dialog(ft.SnackBar(content=ft.Text(str(exc), color="#FFFFFF")))
            return
        self.adapter_url = adapter_link(self.page, self.session.id)
        self.dialog = ft.AlertDialog(modal=True, bgcolor="#FFFFFF", scrollable=True,
                                     shape=ft.RoundedRectangleBorder(radius=8),
                                     title=self.text("Finish with your browser wallet", 18, INK, ft.FontWeight.W_700))
        self.is_open = True
        self.refresh(update=False)
        self.page.show_dialog(self.dialog)
        self.page.run_task(self.poll)

    def close(self, _=None) -> None:
        self.is_open = False
        confirmed = self.session is not None and self.session.state == "confirmed"
        if self.session is not None:
            self.sessions.close(self.session.id)
        self.page.pop_dialog()
        if confirmed:
            self.page.run_task(self.inbox.refresh_transfer)

    async def poll(self) -> None:
        started, tick = time.monotonic(), 0
        while self.is_open and time.monotonic() - started < POLL_LIMIT_SECONDS:
            await asyncio.sleep(POLL_SECONDS)
            if not self.is_open:
                return
            tick += 1
            try:
                session = self.sessions.get(self.session.id)
                if session.state == "submitted" and tick % 2 == 0:
                    await self.sessions.verify(session.id)
            except SessionNotFound:
                self.expired = True
                self.refresh()
                return
            if session.version != self.seen or session.state == "sending":
                self.refresh()
            if session.state in FINAL:
                return
        if self.is_open:
            self.timed_out = True
            self.refresh()

    def refresh(self, *, update: bool = True) -> None:
        if self.dialog is None:
            return
        self.seen = self.session.version if self.session else -1
        self.dialog.content = ft.Container(self.body(), width=460)
        self.dialog.actions = [ft.TextButton("Close", on_click=self.close, style=ft.ButtonStyle(color=ACCENT))]
        if update and self.is_open:
            self.page.update()

    # Actions -------------------------------------------------------------------

    def tab_opened(self, _=None) -> None:
        self.opened_tab = True
        self.refresh()

    async def act(self, operation) -> None:
        if self.busy:
            return
        self.busy = True
        self.local_error = ""
        self.refresh()
        try:
            await operation()
        except SessionNotFound:
            self.expired = True
        except (SessionConflict, ValueError) as exc:
            self.local_error = str(exc)
        finally:
            self.busy = False
            self.refresh()

    async def check_again(self, _=None) -> None:
        await self.act(lambda: self.sessions.review(self.session.id))

    async def send(self, _=None) -> None:
        await self.act(lambda: self.sessions.approve(self.session.id))

    async def verify(self, _=None) -> None:
        await self.act(lambda: self.sessions.verify(self.session.id))

    async def verify_pasted(self, _=None) -> None:
        async def run():
            self.sessions.record_hash(self.session.id, hash32((self.hash_input.value or "").strip()))
            await self.sessions.verify(self.session.id)
        await self.act(run)

    async def release(self, _=None) -> None:
        async def run():
            self.sessions.release(self.session.id)
        await self.act(run)

    # Rendering -----------------------------------------------------------------
    # Hairlines instead of cards, one primary action per state, and mono for every
    # hash, address and amount, matching the ledger inspector.

    def text(self, value, size=13, color=INK, weight=None, *, mono=False, **kwargs):
        return ft.Text(str(value), size=size, color=color, weight=weight,
                       font_family=MONO if mono else None, **kwargs)

    def fact(self, label, value, *, mono=True):
        if callable(getattr(self.inbox, "fact", None)):
            return self.inbox.fact(label, value, mono=mono)
        return ft.Column([self.text(label, 12, INK_3), self.text(value, 13, INK, mono=mono, selectable=True)],
                         spacing=2)

    def link(self, label, url):
        if callable(getattr(self.inbox, "link", None)):
            return self.inbox.link(label, url=url, icon=ft.Icons.OPEN_IN_NEW_ROUNDED)
        return ft.TextButton(label, url=url, icon=ft.Icons.OPEN_IN_NEW_ROUNDED, style=ft.ButtonStyle(color=ACCENT))

    def rule(self):
        return ft.Divider(color=RULE, height=1)

    def note(self, value: str, color: str):
        return ft.Container(self.text(value, 12.5, color, ft.FontWeight.W_600),
                            border=ft.Border(left=ft.BorderSide(2, color)),
                            padding=ft.Padding.only(left=10, top=2, bottom=2))

    def progress(self, label: str):
        return [ft.ProgressBar(color=ACCENT, bgcolor=RULE, bar_height=2), self.text(label, 12.5, INK_2)]

    def body(self):
        s = self.session
        controls = [self.stepper(), self.rule(),
                    self.fact("Original transfer", display_amount(self.item.get("amount")) + " USDC"),
                    self.fact("Network", "Arc Testnet", mono=False)]
        if self.expired:
            controls.append(self.note("This wallet session expired. Close it and start again from the transfer.", RED))
            return ft.Column(controls, spacing=12, tight=True)
        state = s.state
        if s.account:
            controls.append(self.fact("Connected wallet", s.account))
        controls.append(self.rule())
        if state == "created":
            controls.extend(self.connect_controls())
        elif state == "connected" or (state == "reviewed" and s.reviewing):
            if s.reviewing:
                controls.extend(self.progress("Rechecking the transfer and simulating the mint from this address…"))
            if s.error:
                controls.extend([self.note(s.error, RED), self.inbox.button("Check again", self.check_again,
                                                                            disabled=self.busy)])
        elif state == "reviewed":
            controls.extend(self.review_controls())
        elif state == "approving":
            controls.extend(self.progress("Rechecking the transfer before your wallet opens…"))
        elif state in {"approved", "sending"}:
            controls.extend(self.sending_controls())
        elif state == "submitted":
            controls.extend(self.submitted_controls())
        elif state == "confirmed":
            controls.extend(self.confirmed_controls())
        elif state == "failed":
            controls.append(self.note(s.error, RED))
        elif state in {"unknown", "unrelated"}:
            controls.extend(self.unresolved_controls())
        if s.notice:
            controls.append(self.note(s.notice, AMBER))
        if self.local_error:
            controls.append(self.note(self.local_error, RED))
        return ft.Column(controls, spacing=12, tight=True)

    def stepper(self):
        current = STEP_OF.get(self.session.state if self.session else "created", 0)
        cells = []
        for index, label in enumerate(STEPS):
            done, active = index < current, index == current
            cells.append(ft.Row([
                self.text(f"{index + 1}", 12, ACCENT if done or active else INK_3, mono=True),
                self.text(label, 12.5, INK if active else INK_2 if done else INK_3,
                          ft.FontWeight.W_700 if active else None),
            ], spacing=5, tight=True))
        return ft.Row(cells, spacing=16, wrap=True)

    def connect_controls(self):
        ui = self.inbox
        controls = [self.text("Rebound opens a wallet tab. It connects your wallet extension and switches it to "
                              "Arc Testnet. You review and approve the transaction here; the tab only passes it to "
                              "your wallet.", 13, INK_2)]
        connect = ui.button("Open wallet tab again" if self.opened_tab else "Connect wallet", self.tab_opened,
                            primary=not self.opened_tab, icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED)
        # The client opens the tab inside the click, so browsers do not block it as a popup.
        connect.url = ft.Url(self.adapter_url, target=ft.UrlTarget.BLANK)
        if self.opened_tab:
            controls.extend(self.progress("Waiting for your wallet. Approve the connection and Arc Testnet in the wallet tab."))
        controls.extend([connect, self.text("No private key or seed phrase is requested. Testnet only.", 12, INK_3)])
        return controls

    def review_controls(self):
        ui, review = self.inbox, self.session.review
        shortfall = review["shortfall_usdc"] != "0"
        controls = [
            self.fact("Fixed recipient", review["recipient"]),
            self.fact("Original amount", display_amount(review["amount"]) + " USDC"),
            self.fact("Protocol fee", display_amount(review["fee"]) + " USDC"),
            self.fact("Recipient receives", display_amount(review["net_amount"]) + " USDC"),
            self.rule(),
            self.fact("Gas limit", f"{review['gas_limit']:,} units"),
            self.fact("Max fee per gas", gwei(review["max_fee_per_gas"])),
            self.fact("Maximum network fee", review["max_network_fee_usdc"] + " testnet USDC"),
            self.text("Your wallet may show a lower final fee; it cannot exceed this maximum unless you edit it in the wallet.",
                      12, INK_2),
            self.fact("Your Arc balance", review["caller_balance_usdc"] + " testnet USDC"),
        ]
        if shortfall:
            controls.extend([
                self.note(f"Add {review['shortfall_usdc']} testnet USDC to this account to cover the maximum network fee.", AMBER),
                self.link("Open Circle testnet faucet", FAUCET),
            ])
        if self.session.error:
            controls.append(self.note(self.session.error, RED))
        controls.append(ft.Row([
            ui.button("Sending…" if self.busy else "Send from wallet", self.send, primary=True,
                      disabled=self.busy or shortfall),
            ui.button("Check again", self.check_again, disabled=self.busy),
        ], wrap=True, spacing=8))
        if review.get("warning"):
            controls.append(self.text(review["warning"], 12, AMBER))
        return controls

    def sending_controls(self):
        if self.session.state == "sending":
            self.sending_since = self.sending_since or time.monotonic()
        controls = self.progress("Confirm the transaction in your wallet. The wallet tab passes it the reviewed "
                                 "transaction with the gas limit and fee caps shown in the review.")
        if self.sending_since and time.monotonic() - self.sending_since > SENDING_PATIENCE_SECONDS:
            controls.append(self.note("No answer from the wallet tab yet. If you confirmed, find the transaction in "
                                      "your wallet's activity before doing anything else.", AMBER))
            controls.extend(self.recovery_tools())
        return controls

    def submitted_controls(self):
        s = self.session
        controls = self.progress("Submitted. Waiting for a canonical Arc receipt that carries this transfer's message.")
        controls.extend([self.fact("Submitted transaction (not yet verified)", s.hash),
                         self.link("Open submitted transaction", f"{ARC_EXPLORER}/tx/{s.hash}")])
        if self.timed_out:
            controls.extend([self.note("Stopped checking automatically. Check again before any new attempt.", AMBER),
                             self.inbox.button("Check receipt again", self.verify, disabled=self.busy)])
        return controls

    def confirmed_controls(self):
        receipt = self.session.receipt
        controls = [
            self.note("Transfer delivered. Rebound verified this exact Arc transaction against the original transfer: "
                      "canonical block, consumed nonce, and a MessageReceived event with this transfer's nonce and "
                      "message body.", ACCENT),
            self.fact("Verified Arc transaction", receipt["tx_hash"]),
            self.fact("Arc block", f"{receipt['block']:,}"),
        ]
        if receipt.get("fee_paid_usdc"):
            controls.append(self.fact("Network fee paid", receipt["fee_paid_usdc"] + " testnet USDC"))
        controls.append(self.link("Open verified Arc receipt", receipt["url"]))
        return controls

    def unresolved_controls(self):
        s = self.session
        controls = [self.note(s.error, AMBER if s.state == "unknown" else RED)]
        if s.state == "unrelated":
            controls.append(self.inbox.button("Check this hash again", self.verify, disabled=self.busy))
        controls.extend(self.recovery_tools())
        return controls

    def recovery_tools(self):
        ui = self.inbox
        return [
            self.hash_input,
            ft.Row([ui.button("Verify this hash", self.verify_pasted, primary=True, disabled=self.busy),
                    ui.button("My wallet sent nothing", self.release, disabled=self.busy)], wrap=True, spacing=8),
            self.text("Release only after checking the wallet's activity. Another send for this transfer stays blocked "
                      "on this server until this one is resolved.", 12, INK_3),
        ]
