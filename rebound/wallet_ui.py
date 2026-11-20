"""Flet review and status panel for finishing one ready transfer from a browser wallet.

Everything the user reads or approves is rendered here in Python, in the ledger style
of docs/DESIGN.md. The wallet tab (rebound/wallet.py) only connects the wallet and
passes it the stored transaction.
"""
import asyncio
import time
from urllib.parse import urlsplit, urlunsplit

import flet as ft

from rebound.protocol import ARC_EXPLORER, hash32
from rebound.ui import (
    INK,
    INK_2,
    INK_3,
    RED,
    RULE,
    SURFACE,
    TONES,
    aligned_amounts,
    display_amount,
    field_style,
    shorten,
)
from rebound.wallet_session import FINAL, SessionConflict, SessionNotFound, sessions_for

FAUCET = "https://faucet.circle.com/"
POLL_SECONDS = 1.5
POLL_LIMIT_SECONDS = 15 * 60
SENDING_PATIENCE_SECONDS = 180
STEPS = ("Connect", "Review", "Send", "Verify")
STEP_OF = {"created": 0, "connected": 1, "reviewed": 1, "approving": 2, "approved": 2, "sending": 2,
           "submitted": 3, "unknown": 3, "unrelated": 3, "confirmed": 3, "failed": 3}
UNIT_WIDTH = 44  # Right-hand unit column, so every figure in the dialog ends on one edge.
SENT_MAYBE = frozenset({"sending", "submitted", "unknown", "unrelated"})


def adapter_link(page, code: str) -> str | None:
    """The wallet-tab URL. The one-time code rides in the fragment, which never reaches a server."""
    page_url = urlsplit(getattr(page, "url", "") or "")
    scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(page_url.scheme)
    if scheme is None or not page_url.netloc:
        return None
    return urlunsplit((scheme, page_url.netloc, f"{page_url.path.rstrip('/')}/wallet/adapter", "", code))


def wallet_available(inbox, item: dict) -> bool:
    """Offered only when enabled, for live verified ready transfers, on a server that can verify receipts."""
    service = inbox.service
    return (getattr(getattr(service, "settings", None), "browser_wallet", False) is True
            and item.get("status") == "ready" and not inbox.state.example and not item.get("example")
            and not str(item.get("key", "")).startswith("example-")
            and all(callable(getattr(service, name, None)) for name in ("prepare", "inspect_transaction"))
            and adapter_link(inbox.page, "x") is not None)


def wallet_button(inbox, item: dict):
    if not wallet_available(inbox, item):
        return None
    return inbox.button("Finish with browser wallet", lambda _: WalletPanel(inbox, item).open(), primary=True)


def gwei(wei: int) -> str:
    """Exact gwei: keeps integer zeroes (50 stays 50) and trims only fractional ones."""
    whole, fraction = divmod(int(wei), 10**9)
    return f"{whole:,}" + (f".{fraction:09d}".rstrip("0") if fraction else "")


class WalletPanel:
    def __init__(self, inbox, item: dict):
        self.inbox = inbox
        self.page = inbox.page
        self.item = item
        self.sessions = sessions_for(inbox.service)
        self.session = None
        self.dialog = None
        self.is_open = False
        self.opened_tab = False
        self.busy = False
        self.expired = False
        self.timed_out = False
        self.local_error = ""
        self.copied = None
        self.seen = -1
        self.sending_since = None
        self.shown = None  # (generation, transaction) of the review on screen.
        # One field for the panel's life, so status re-renders never drop a typed hash.
        self.hash_input = ft.TextField(hint_text="Transaction hash from your wallet's activity, 0x…",
                                       **field_style())

    # Lifecycle -----------------------------------------------------------------

    def open(self) -> None:
        try:
            # Reopening a transfer whose attempt is still unresolved resumes that same session.
            self.session = self.sessions.open(self.item["tx_hash"], self.item["log_index"])
        except (SessionConflict, ValueError) as exc:
            self.page.show_dialog(ft.SnackBar(content=self.inbox.text(str(exc), color=SURFACE)))
            return
        self.opened_tab = self.session.state != "created"
        self.dialog = ft.AlertDialog(modal=True, bgcolor=SURFACE, scrollable=True,
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
            self.sessions.close(self.session.id)  # Kept while it holds the transfer.
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
        self.dialog.actions = [self.inbox.link("Close", self.close)]
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
        if self.shown is None:
            return
        generation, transaction = self.shown
        await self.act(lambda: self.sessions.approve(self.session.id, generation=generation,
                                                     transaction=transaction))

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

    async def copy(self, value: str) -> None:
        await self.inbox.clipboard.set(value)
        self.copied = value
        self.refresh()

    # Rendering -----------------------------------------------------------------
    # Ledger rules: hairlines instead of boxes, one primary action per state, color
    # only for state, and mono figures that end on one right-hand edge.

    def text(self, value, size=13, color=INK, weight=None, *, mono=False, **kwargs):
        return self.inbox.text(value, size, color, weight, mono=mono, **kwargs)

    def link(self, label, url):
        return ft.Row([self.inbox.link(label, url=url, icon=ft.Icons.OPEN_IN_NEW, flush=True)])

    def line(self, value, color=INK_2, size=12.5):
        return self.text(value, size, color)

    def error(self, value):
        return self.text(value, 13, RED, ft.FontWeight.W_600)

    def rule(self):
        return ft.Divider(color=RULE, height=1, thickness=1)

    def progress(self, label: str):
        return [ft.ProgressBar(color=TONES["action"], bgcolor=RULE, bar_height=2), self.line(label)]

    def outcome(self, label, tone):
        """Status as a dot and a word, as in the ledger."""
        return ft.Row([ft.Container(width=7, height=7, border_radius=4, bgcolor=TONES[tone]),
                       self.text(label, 13, INK, ft.FontWeight.W_700)], spacing=8, tight=True)

    def figure(self, label, value, unit="", *, total=False, first=False):
        """One right-aligned row: label, mono figure, unit column."""
        return ft.Container(ft.Row([
            self.text(label, 13, INK if total else INK_2, ft.FontWeight.W_700 if total else None, expand=True),
            self.text(value, 13, INK, ft.FontWeight.W_600 if total else None, mono=True, selectable=True,
                      text_align=ft.TextAlign.RIGHT),
            ft.Container(self.text(unit, 12, INK_3), width=UNIT_WIDTH, padding=ft.Padding.only(left=8)),
        ], spacing=0), padding=ft.Padding.symmetric(vertical=6),
            border=None if first else ft.Border(top=ft.BorderSide(1, INK if total else RULE)))

    def table(self, title, rows, unit=""):
        header = ft.Row([self.text(title, 13, INK, ft.FontWeight.W_700, expand=True),
                         ft.Container(self.text(unit, 12, INK_3), width=UNIT_WIDTH, padding=ft.Padding.only(left=8))],
                        spacing=0)
        return ft.Column([header, ft.Column(rows, spacing=0)], spacing=2)

    def address(self, label, value):
        return ft.Column([self.text(label, 12, INK_3), self.text(value, 12.5, INK, mono=True, selectable=True)],
                         spacing=2)

    def hash_fact(self, label, value):
        """Middle-truncated hash; the full value is in the tooltip and one click from the clipboard."""
        copied = self.copied == value
        return ft.Column([self.text(label, 12, INK_3), ft.Row([
            self.text(shorten(value, 8), 12.5, INK, mono=True, selectable=True, tooltip=value),
            self.inbox.link("Copied" if copied else "Copy", lambda _: self.page.run_task(self.copy, value),
                            icon=ft.Icons.CONTENT_COPY, flush=True),
        ], spacing=12, tight=True)], spacing=2)

    def stepper(self):
        current = STEP_OF.get(self.session.state if self.session else "created", 0)
        cells = []
        for index, label in enumerate(STEPS):
            color = INK if index == current else INK_3
            weight = ft.FontWeight.W_700 if index == current else None
            cells.append(ft.Row([self.text(index + 1, 12, color, weight, mono=True),
                                 self.text(label, 12.5, color, weight)], spacing=6, tight=True))
        return ft.Row(cells, spacing=18, wrap=True)

    def body(self):
        s = self.session
        if self.expired:
            return ft.Column([self.error("This wallet session expired. Close it and start again from the transfer.")],
                             tight=True)
        state = s.state
        controls = [self.stepper(), self.rule()]
        if s.account:
            controls.append(self.address("Connected wallet", s.account))
        if state == "created":
            controls.extend(self.connect_controls())
        elif state == "connected" or (state == "reviewed" and s.reviewing):
            controls.append(self.figure("Original transfer", display_amount(self.item.get("amount")), "USDC",
                                        first=True))
            if s.reviewing:
                controls.extend(self.progress("Rechecking the transfer and simulating the mint from this address…"))
            if s.error:
                controls.extend([self.error(s.error), ft.Row([self.inbox.button(
                    "Check again", self.check_again, disabled=self.busy)])])
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
            controls.extend([self.outcome("Not delivered", "stop"), self.line(s.error)])
        elif state in {"unknown", "unrelated"}:
            controls.extend(self.unresolved_controls())
        if state in SENT_MAYBE:
            controls.extend(self.recovery_tools())
        if s.notice:
            controls.append(self.line(s.notice))
        if self.local_error:
            controls.append(self.error(self.local_error))
        return ft.Column(controls, spacing=12, tight=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def connect_controls(self):
        ui = self.inbox
        controls = [
            self.figure("Original transfer", display_amount(self.item.get("amount")), "USDC", first=True),
            self.line("Rebound opens a wallet tab. It connects your wallet extension and switches it to Arc Testnet. "
                      "You review and approve the transaction here; the tab only passes it to your wallet.", INK_2, 13),
        ]
        connect = ui.button("Open wallet tab again" if self.opened_tab else "Connect wallet", self.tab_opened,
                            primary=not self.opened_tab)
        # The client opens the tab inside the click, so browsers do not block it as a popup.
        connect.url = ft.Url(adapter_link(self.page, self.session.code), target=ft.UrlTarget.BLANK)
        if self.opened_tab:
            controls.extend(self.progress("Waiting for your wallet. Approve the connection and Arc Testnet in the wallet tab."))
        controls.extend([ft.Row([connect]),
                         self.line("No private key or seed phrase is requested. Testnet only.", INK_3, 12)])
        return controls

    def review_controls(self):
        s = self.session
        review = s.review
        self.shown = (s.generation, dict(s.transaction))
        shortfall = review["shortfall_usdc"] != "0"
        amount, fee, net = aligned_amounts(review["amount"], review["fee"], review["net_amount"])
        controls = [
            self.address("Fixed recipient", review["recipient"]),
            self.table("Amount", [
                self.figure("Original amount", amount, first=True),
                self.figure("Protocol fee", fee),
                self.figure("Recipient receives", net, total=True),
            ], "USDC"),
            self.table("Network fee", [
                self.figure("Gas limit", f"{review['gas_limit']:,}", "units", first=True),
                self.figure("Max fee per gas", gwei(review["max_fee_per_gas"]), "gwei"),
                self.figure("Maximum network fee", review["max_network_fee_usdc"], "USDC", total=True),
                self.figure("Your Arc balance", review["caller_balance_usdc"], "USDC", first=True),
            ]),
            self.line("Your wallet may show a lower final fee; it cannot exceed this maximum unless you edit it in "
                      "the wallet.", INK_3, 12),
        ]
        if shortfall:
            controls.extend([
                self.line(f"Add {review['shortfall_usdc']} testnet USDC to this account to cover the maximum "
                          "network fee.", INK, 13),
                self.link("Open Circle testnet faucet", FAUCET),
            ])
        if s.error:
            controls.append(self.error(s.error))
        controls.append(ft.Row([
            self.inbox.button("Sending…" if self.busy else "Send from wallet", self.send, primary=True,
                              disabled=self.busy or shortfall),
            self.inbox.button("Check again", self.check_again, disabled=self.busy),
        ], wrap=True, spacing=8))
        if review.get("warning"):
            controls.append(self.line(review["warning"], INK_3, 12))
        return controls

    def sending_controls(self):
        if self.session.state == "sending":
            self.sending_since = self.sending_since or time.monotonic()
        controls = self.progress("Confirm the transaction in your wallet. The wallet tab passes it the reviewed "
                                 "transaction with the gas limit and fee caps shown in the review.")
        if self.sending_since and time.monotonic() - self.sending_since > SENDING_PATIENCE_SECONDS:
            controls.append(self.line("No answer from the wallet tab yet. If you confirmed, paste the hash from your "
                                      "wallet's activity below."))
        return controls

    def submitted_controls(self):
        s = self.session
        controls = self.progress("Submitted. Waiting for a canonical Arc receipt that carries this transfer's message.")
        controls.extend([self.hash_fact("Submitted transaction, not yet verified", s.hash),
                         self.link("Open submitted transaction", f"{ARC_EXPLORER}/tx/{s.hash}")])
        if self.timed_out:
            controls.extend([self.line("Stopped checking automatically. Check again before any new attempt."),
                             ft.Row([self.inbox.button("Check receipt again", self.verify, disabled=self.busy)])])
        return controls

    def confirmed_controls(self):
        receipt = self.session.receipt
        rows = [self.figure("Arc block", f"{receipt['block']:,}", first=True)]
        if receipt.get("fee_paid_usdc"):
            rows.append(self.figure("Network fee paid", receipt["fee_paid_usdc"], "USDC"))
        return [
            self.outcome("Transfer delivered", "ok"),
            self.line("Rebound verified this exact Arc transaction against the original transfer: canonical block, "
                      "consumed nonce, and a MessageReceived event with this transfer's nonce and message body."),
            self.hash_fact("Verified Arc transaction", receipt["tx_hash"]),
            ft.Column(rows, spacing=0),
            self.link("Open verified Arc receipt", receipt["url"]),
        ]

    def unresolved_controls(self):
        s = self.session
        if s.state == "unknown":
            return [self.outcome("Outcome unknown", "wait"), self.line(s.error)]
        controls = [self.outcome("Not proven", "stop"), self.error(s.error)]
        if s.hash:
            controls.append(self.hash_fact("Checked transaction", s.hash))
        controls.append(ft.Row([self.inbox.button("Check this hash again", self.verify, disabled=self.busy)]))
        return controls

    def recovery_tools(self):
        """Kept mounted whenever a send may have happened. Release is offered only once the
        wallet tab can no longer submit (unknown or unrelated), never while approving or sending."""
        s = self.session
        self.hash_input.disabled = self.busy
        buttons = [self.inbox.button("Verify this hash", self.verify_pasted,
                                     primary=s.state in {"unknown", "unrelated"}, disabled=self.busy)]
        controls = [self.rule(), self.text("Have a transaction hash?", 13, INK, ft.FontWeight.W_700),
                    self.hash_input]
        if s.state in {"unknown", "unrelated"}:
            buttons.append(self.inbox.button("My wallet sent nothing", self.release, disabled=self.busy))
        controls.append(ft.Row(buttons, wrap=True, spacing=8))
        if s.state in {"unknown", "unrelated"}:
            controls.append(self.line("Release only after checking the wallet's activity and rejecting any pending "
                                      "request. Another send for this transfer stays blocked on this server until "
                                      "this one is resolved.", INK_3, 12))
        return controls
