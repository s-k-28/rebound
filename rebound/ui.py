"""Python/Flutter transfer inbox; financial state is supplied by RecoveryService."""
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import flet as ft

from rebound.examples import example_transfers
from rebound.protocol import ZERO, address

INK = "#172823"
MUTED = "#718078"
BLUE = "#28624F"
GROUND = "#F4F6F3"
LINE = "#E0E6E0"
GREEN = "#28624F"
AMBER = "#A96714"
FOREST = "#173B32"
MINT = "#C9E8D8"

STATE_COPY = {
    "ready": ("Ready to finish", "A signed message is available. Rebound will recheck it before preparing the destination mint.", BLUE, ft.Icons.NORTH_EAST_ROUNDED),
    "awaiting_attestation": ("Waiting for Circle", "Your source transfer was found. Wait for Circle's attestation, then scan again.", AMBER, ft.Icons.SCHEDULE_ROUNDED),
    "source_confirming": ("Source confirming", "The source transfer needs more confirmations before recovery can be prepared.", AMBER, ft.Icons.SCHEDULE_ROUNDED),
    "completed": ("Completed", "The destination has already consumed this message's nonce. Do not send the transfer again.", GREEN, ft.Icons.CHECK_CIRCLE_ROUNDED),
    "caller_required": ("Specific caller required", "Only the destination caller named in the original transfer can deliver this message. The recipient cannot change that rule.", AMBER, ft.Icons.LOCK_OUTLINE_ROUNDED),
    "expired": ("Attestation expired", "The original burn still exists. It needs a fresh Circle attestation before it can be completed.", AMBER, ft.Icons.UPDATE_ROUNDED),
    "unsupported": ("Outside current support", "Rebound cannot safely prepare this transfer with the current route and message support.", MUTED, ft.Icons.INFO_OUTLINE_ROUNDED),
    "unavailable": ("Could not verify", "A network or data source is unavailable. Try again before taking any action.", MUTED, ft.Icons.CLOUD_OFF_OUTLINED),
    "source_changed": ("Source needs review", "The source receipt no longer matches the indexed event. Rebound will not prepare a recovery.", AMBER, ft.Icons.WARNING_AMBER_ROUNDED),
    "submitted": ("Mint submitted", "A destination transaction was submitted. Check its receipt before attempting another mint.", BLUE, ft.Icons.SCHEDULE_ROUNDED),
}


def state_copy(status: str) -> tuple:
    return STATE_COPY.get(status, STATE_COPY["unavailable"])


def display_amount(value: str | None) -> str:
    if value is None:
        return "Not available yet"
    amount = Decimal(value)
    return f"{amount:,.2f}" if amount == amount.quantize(Decimal("0.01")) else f"{amount:,f}"


def shorten(value: str | None, size: int = 6) -> str:
    if not value:
        return "Not available yet"
    return value if len(value) < size * 2 + 4 else f"{value[:size + 2]}…{value[-size:]}"


def safe_error(exc: Exception) -> str:
    if isinstance(exc, ValueError) or type(exc).__name__ == "UpstreamError":
        return str(exc)
    return "The transfer could not be verified. Try again or use a narrower history range."


@dataclass
class InboxState:
    transfers: list[dict] = field(default_factory=list)
    example: bool = False
    filter: str = "all"
    busy: bool = False
    scanned: bool = False
    error: str = ""
    coverage: str = ""
    partial: bool = False
    wallet: str = ""
    selected: dict | None = None
    prepared: dict | None = None
    query: str = ""

    def load_examples(self) -> None:
        self.transfers = example_transfers()
        self.example = True
        self.scanned = True
        self.filter = "all"
        self.query = ""
        self.error = ""
        self.coverage = "Four synthetic transfers show how recovery states work. No live funds are represented."
        self.selected = None
        self.prepared = None

    def visible_transfers(self) -> list[dict]:
        items = self.transfers
        if self.filter == "completed":
            items = [item for item in items if item["status"] == "completed"]
        elif self.filter == "attention":
            items = [item for item in items if item["status"] != "completed"]
        query = self.query.strip().lower()
        if query:
            items = [item for item in items if query in " ".join(str(item.get(key) or "") for key in
                     ("tx_hash", "recipient", "depositor", "amount", "status")).lower()]
        return items

    def counts(self) -> dict:
        return {"all": len(self.transfers),
                "attention": sum(item["status"] != "completed" for item in self.transfers),
                "ready": sum(item["status"] == "ready" for item in self.transfers),
                "completed": sum(item["status"] == "completed" for item in self.transfers)}

    async def scan(self, service: Any, wallet: str, blocks: int) -> None:
        self.busy = True
        self.example = False
        self.scanned = False
        self.error = ""
        self.coverage = ""
        self.partial = False
        self.transfers = []
        self.selected = None
        self.prepared = None
        self.filter = "all"
        try:
            self.wallet = address(wallet.strip())
            result = await service.scan(self.wallet, blocks=blocks)
            self.transfers = result["transfers"]
            self.coverage = result.get("coverage") or f"Base Sepolia blocks {result['from_block']:,}–{result['to_block']:,}."
            self.partial = bool(result.get("partial"))
            self.scanned = True
        except Exception as exc:
            self.error = safe_error(exc)
        finally:
            self.busy = False

    async def prepare(self, service: Any, transfer: dict, caller: str) -> dict:
        if self.example or transfer.get("example") or str(transfer.get("key", "")).startswith("example-"):
            raise ValueError("This is an example. Recovery transactions are only available for verified live transfers.")
        if transfer.get("status") not in {"ready", "caller_required"}:
            raise ValueError("Only a ready transfer can be prepared. Refresh its state first.")
        signer = address(caller.strip())
        self.prepared = None
        self.prepared = await service.prepare(transfer["tx_hash"], transfer["log_index"], signer)
        return self.prepared


class InboxUI:
    def __init__(self, page: ft.Page, service: Any):
        self.page = page
        self.service = service
        self.state = InboxState()
        self.wallet_input = ft.TextField(
            label="Public wallet address", hint_text="0x…", text_size=15,
            prefix_icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
            filled=True, fill_color="#FFFFFF", border_color=LINE,
            focused_border_color=BLUE, border_radius=12, content_padding=18,
            autocorrect=False, on_submit=self.scan,
        )
        self.history = ft.Dropdown(
            value="2000", label="Recent history", width=230, text_size=13,
            options=[ft.DropdownOption("2000", "2,000 source blocks"),
                     ft.DropdownOption("10000", "10,000 source blocks"),
                     ft.DropdownOption("50000", "50,000 source blocks")],
            filled=True, fill_color="#FFFFFF", border_color=LINE, border_radius=10,
        )
        self.caller_input = ft.TextField(
            label="Address that will sign the destination transaction", hint_text="0x…",
            text_size=14, filled=True, fill_color="#FFFFFF", border_color=LINE,
            focused_border_color=BLUE, border_radius=10, autocorrect=False,
        )
        self.review_error = ""
        self.preparing = False
        self.root = ft.Row(expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        self.search_input = ft.TextField(hint_text="Search transfers", prefix_icon=ft.Icons.SEARCH_ROUNDED,
            text_size=12, height=40, width=210, filled=True, fill_color="#F4F6F3",
            border_color="transparent", border_radius=8, content_padding=10,
            on_change=self.search, autocorrect=False)
        self.ledger_host = ft.Container()
        self.inspector_host = ft.Container(width=342)
        self.clipboard = ft.Clipboard()

    def install(self) -> None:
        self.page.title = "Rebound · Transfer inbox"
        self.page.bgcolor = GROUND
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.fonts = {"Manrope": "/fonts/Manrope.ttf"}
        self.page.theme = ft.Theme(color_scheme_seed=BLUE, font_family="Manrope", use_material3=True)
        self.page.services.append(self.clipboard)
        self.page.on_resize = self.resize
        self.page.add(self.root)
        self.render()

    def resize(self, _=None) -> None:
        self.render()

    @property
    def narrow(self) -> bool:
        return (self.page.width or 1100) < 720

    def text(self, value, size=14, color=INK, weight=None, **kwargs):
        return ft.Text(str(value), size=size, color=color, weight=weight, **kwargs)

    def button(self, label, callback=None, *, primary=False, icon=None, disabled=False):
        cls = ft.Button if primary else ft.OutlinedButton
        return cls(label, icon=icon, on_click=callback, disabled=disabled,
                   style=ft.ButtonStyle(
                       bgcolor=BLUE if primary else "#FFFFFF",
                       color="#FFFFFF" if primary else INK,
                       shape=ft.RoundedRectangleBorder(radius=10),
                       padding=ft.Padding.symmetric(horizontal=20, vertical=16),
                       text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
                       side=ft.BorderSide(0 if primary else 1, LINE),
                   ), elevation=0) if primary else cls(
                       label, icon=icon, on_click=callback, disabled=disabled,
                       style=ft.ButtonStyle(
                           color=INK, bgcolor="#FFFFFF", side=ft.BorderSide(1, LINE),
                           shape=ft.RoundedRectangleBorder(radius=10),
                           padding=ft.Padding.symmetric(horizontal=18, vertical=15),
                           text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
                       ))

    def badge(self, label, color=BLUE, background="#EAF3ED"):
        return ft.Container(self.text(label, 12, color, ft.FontWeight.W_600),
                            bgcolor=background, border_radius=20,
                            padding=ft.Padding.symmetric(horizontal=12, vertical=7))

    def pill(self, status):
        title, _, color, icon = state_copy(status)
        bg = "#E6F1E8" if status == "completed" else "#FFF3DF" if color == AMBER else "#EAF3ED" if color == BLUE else "#EEF2F6"
        return ft.Container(ft.Row([ft.Icon(icon, size=15, color=color), self.text(title, 12, color, ft.FontWeight.W_600)], spacing=6, tight=True),
                            bgcolor=bg, border_radius=20, padding=ft.Padding.symmetric(horizontal=11, vertical=8))

    def panel(self, content, padding=24, **kwargs):
        return ft.Container(content, bgcolor="#FFFFFF", border=ft.Border.all(1, LINE),
                            border_radius=16, padding=padding, **kwargs)

    def render(self) -> None:
        self.wallet_input.disabled = self.state.busy
        self.history.disabled = self.state.busy
        self.search_input.value = self.state.query
        content = [self.workspace_heading(), self.scan_panel(), self.metrics()]
        if self.state.example:
            content.insert(0, ft.Container(ft.Row([
                ft.Icon(ft.Icons.SCIENCE_OUTLINED, color=AMBER, size=17),
                self.text("Example workspace · synthetic transfers, no live funds or signing", 12, AMBER, expand=True),
                ft.TextButton("Return to live", on_click=self.return_live, style=ft.ButtonStyle(color=AMBER)),
            ]), bgcolor="#FFF3DF", padding=ft.Padding.symmetric(horizontal=16, vertical=4), border_radius=9))
        if (self.page.width or 1200) >= 1180:
            self.inspector_host.content = self.inspector() if self.state.selected else self.recovery_note()
            content.append(ft.Row([
                ft.Container(self.inbox(), expand=True),
                self.inspector_host,
            ], spacing=20, vertical_alignment=ft.CrossAxisAlignment.START))
        elif self.state.selected:
            content.append(self.inspector())
        else:
            content.append(self.inbox())
        content.append(self.footer())
        body = ft.Column(content, spacing=22, scroll=ft.ScrollMode.AUTO, expand=True,
                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        main = ft.Column([self.header(), ft.Container(body, padding=20 if self.narrow else 30, expand=True)],
                         spacing=0, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.root.controls = ([self.sidebar()] if not self.narrow else []) + [main]
        self.page.update()

    def logo(self, *, dark=False):
        return ft.Row([
            ft.Container(ft.Icon(ft.Icons.TURN_SLIGHT_RIGHT_ROUNDED, color=FOREST if dark else "#FFFFFF", size=25),
                         bgcolor=MINT if dark else FOREST, width=35, height=35, border_radius=11, alignment=ft.Alignment.CENTER),
            self.text("rebound", 25, "#FFFFFF" if dark else INK, ft.FontWeight.W_700),
        ], spacing=10, tight=True)

    def sidebar(self):
        counts = self.state.counts()
        def item(label, icon, key):
            active = self.state.filter == key
            return ft.Container(ft.Row([
                ft.Icon(icon, size=19, color=MINT if active else "#AFC3B9"),
                self.text(label, 13, "#FFFFFF" if active else "#C3D2CA", ft.FontWeight.W_600 if active else ft.FontWeight.W_500, expand=True),
                self.text(counts[key] if self.state.scanned else "—", 12, MINT if active else "#AFC3B9"),
            ], spacing=11), bgcolor="#2B5044" if active else "transparent", border_radius=9,
                padding=ft.Padding.symmetric(horizontal=12, vertical=13), on_click=lambda _: self.set_filter(key),
                tooltip=label)
        resources = ft.Column([
            ft.TextButton("How recovery works", icon=ft.Icons.HELP_OUTLINE_ROUNDED, on_click=self.about,
                          style=ft.ButtonStyle(color="#B8CCC0", padding=ft.Padding.symmetric(horizontal=11, vertical=13))),
            ft.TextButton("Supported route", icon=ft.Icons.ALT_ROUTE_ROUNDED, on_click=self.coverage,
                          style=ft.ButtonStyle(color="#B8CCC0", padding=ft.Padding.symmetric(horizontal=11, vertical=13))),
        ], spacing=3)
        return ft.Container(ft.Column([
            ft.Container(self.logo(dark=True), padding=ft.Padding.only(left=9, top=5, bottom=31)),
            ft.Container(ft.Row([ft.Container(self.text("R", 13, FOREST, ft.FontWeight.W_700), width=29, height=29,
                             bgcolor="#E0EEE5", border_radius=8, alignment=ft.Alignment.CENTER),
                ft.Column([self.text("Recovery workspace", 12, "#FFFFFF", ft.FontWeight.W_600),
                           self.text("Base Sepolia / Arc", 10, "#AAC1B3")], spacing=3)], spacing=10),
                padding=12, border=ft.Border.all(1, "#39594C"), border_radius=10),
            ft.Container(height=15),
            item("All transfers", ft.Icons.INBOX_OUTLINED, "all"),
            item("Needs attention", ft.Icons.RADIO_BUTTON_CHECKED_ROUNDED, "attention"),
            item("Completed", ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED, "completed"),
            ft.Container(height=19),
            ft.Divider(color="#345449", height=1),
            ft.Container(resources, padding=ft.Padding.only(top=12)),
            ft.Container(expand=True),
            ft.Container(ft.Column([
                ft.Row([ft.Icon(ft.Icons.SCIENCE_OUTLINED, color=MINT, size=19),
                        self.text("See the full workflow", 12, "#FFFFFF", ft.FontWeight.W_600)], spacing=9),
                self.text("Explore a sample inbox before scanning a real address.", 11, "#AAC1B3"),
                ft.TextButton("Explore examples", on_click=self.examples, disabled=self.state.busy,
                    style=ft.ButtonStyle(color=MINT, padding=0)),
            ], spacing=11), padding=15, bgcolor="#244A3D", border_radius=11),
            ft.Row([ft.Container(width=6, height=6, bgcolor=MINT, border_radius=3),
                    self.text("Testnet environment", 10, "#AAC1B3")], spacing=8),
        ], spacing=7, expand=True), width=228, bgcolor=FOREST, padding=18)

    def header(self):
        left = self.logo() if self.narrow else ft.Row([
            self.text("Workspace", 12, MUTED), self.text("/", 13, "#BAC4BC"),
            self.text("Transfers", 12, INK, ft.FontWeight.W_600)], spacing=12, tight=True)
        right = ft.Row([ft.Icon(ft.Icons.ALT_ROUTE_ROUNDED, size=15, color=MUTED),
                        self.text("Base Sepolia", 11, MUTED), ft.Icon(ft.Icons.EAST_ROUNDED, size=13, color=MUTED),
                        self.text("Arc Testnet", 11, MUTED), self.badge("Testnet", FOREST, "#E8F0E9")], spacing=9, tight=True)
        header_content = ft.Column([left, right], spacing=13) if self.narrow else ft.Row([left, ft.Container(expand=True), right])
        return ft.Container(header_content,
                            bgcolor="#FFFFFF", border=ft.Border(bottom=ft.BorderSide(1, LINE)),
                            padding=ft.Padding.symmetric(horizontal=20 if self.narrow else 30, vertical=18))

    def workspace_heading(self):
        return ft.Row([
            ft.Column([self.text("Transfer inbox", 27, INK, ft.FontWeight.W_700),
                       self.text("Find the transfer. Understand its state. Finish the original journey.", 12, MUTED)], spacing=7, expand=True),
            ft.TextButton("Explore examples", icon=ft.Icons.PLAY_CIRCLE_OUTLINE_ROUNDED,
                          on_click=self.examples, disabled=self.state.busy, style=ft.ButtonStyle(color=BLUE)),
        ], spacing=15)

    def scan_panel(self):
        self.wallet_input.label = "Wallet address"
        self.wallet_input.text_size = 13
        self.wallet_input.content_padding = 14
        self.wallet_input.border_radius = 9
        self.wallet_input.height = 51
        self.history.width = 195
        self.history.text_size = 12
        self.history.height = 51
        self.history.content_padding = 13
        button = self.button("Scanning…" if self.state.busy else "Find transfers", self.scan,
                             primary=True, disabled=self.state.busy, icon=ft.Icons.SEARCH_ROUNDED)
        if self.narrow or (self.page.width or 1200) < 1100:
            form = ft.Column([self.wallet_input, ft.Row([self.history, button], wrap=True, spacing=12)],
                             spacing=13, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
            self.wallet_input.expand = False
        else:
            self.wallet_input.expand = True
            form = ft.Row([self.wallet_input, self.history, button], spacing=12)
        controls = [ft.Row([ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, color=BLUE, size=18),
                            self.text("Find transfers by address", 13, INK, ft.FontWeight.W_700),
                            ft.Container(expand=True), self.text("Read-only discovery", 10, MUTED, visible=not self.narrow)], spacing=8),
                    form, ft.Row([ft.Icon(ft.Icons.LOCK_OUTLINE_ROUNDED, color=MUTED, size=13),
                                  self.text("A public address is all you need. Your keys stay with you.", 11, MUTED)], spacing=6)]
        if self.state.busy:
            controls.extend([ft.ProgressBar(color=BLUE, bgcolor="#E8F0E9", bar_height=3),
                             self.text("Reading source events, Circle attestations and destination state…", 11, MUTED)])
        if self.state.error:
            controls.append(ft.Container(ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, color="#AE3434", size=18),
                self.text(self.state.error, 12, "#AE3434", expand=True)]), padding=12, bgcolor="#FFF0EF", border_radius=8))
        return ft.Container(ft.Column(controls, spacing=14), bgcolor="#FFFFFF", padding=20,
                            border_radius=12, border=ft.Border.all(1, LINE))

    def metrics(self):
        counts = self.state.counts()
        labels = [("Transfers found", "all", ft.Icons.INBOX_OUTLINED),
                  ("Ready to finish", "ready", ft.Icons.NORTH_EAST_ROUNDED),
                  ("Completed", "completed", ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED)]
        cells = []
        for index, (label, key, icon) in enumerate(labels):
            inner = ft.Column([self.text(label, 10 if self.narrow else 11, MUTED),
                           self.text(counts[key] if self.state.scanned else "—", 23, INK, ft.FontWeight.W_700)], spacing=4)
            contents = [inner] if self.narrow else [
                ft.Container(ft.Icon(icon, color=BLUE, size=18), bgcolor="#EAF1E9", padding=10, border_radius=9), inner]
            cells.append(ft.Container(ft.Row(contents, spacing=12), expand=True, padding=ft.Padding.only(left=12 if self.narrow and index else 18 if index else 0),
                border=ft.Border(left=ft.BorderSide(1, LINE)) if index else None))
        return ft.Container(ft.Row(cells, spacing=12), padding=ft.Padding.symmetric(vertical=4))

    def inbox(self):
        def tab(label, key):
            active = self.state.filter == key
            return ft.Container(ft.Row([self.text(label, 11, BLUE if active else MUTED, ft.FontWeight.W_700 if active else ft.FontWeight.W_500),
                self.text(self.state.counts()[key] if self.state.scanned else "—", 10, BLUE if active else MUTED)], spacing=7),
                padding=ft.Padding.symmetric(horizontal=12, vertical=12),
                border=ft.Border(bottom=ft.BorderSide(2, BLUE if active else "transparent")),
                on_click=lambda _: self.set_filter(key))
        filters = ft.Row([tab("All transfers", "all"), tab("Needs attention", "attention"), tab("Completed", "completed")], spacing=0, wrap=True)
        compact_tools = self.narrow or bool(self.state.selected and (self.page.width or 1200) < 1300)
        top = ft.Column([filters, self.search_input], spacing=6) if compact_tools else ft.Row([filters, ft.Container(expand=True), self.search_input])
        self.ledger_host.content = self.ledger_content()
        children = [ft.Container(top, padding=ft.Padding.only(left=10, right=14, top=10)),
                    ft.Divider(color=LINE, height=1), self.ledger_host]
        return ft.Container(ft.Column(children, spacing=0), bgcolor="#FFFFFF", border_radius=12,
                            border=ft.Border.all(1, LINE), clip_behavior=ft.ClipBehavior.ANTI_ALIAS)

    def ledger_content(self):
        children = []
        visible = self.state.visible_transfers()
        if visible:
            if not self.narrow:
                children.append(ft.Container(ft.Row([
                    self.text("Transfer", 10, MUTED, expand=True),
                    self.text("Amount", 10, MUTED, width=100), self.text("State", 10, MUTED, width=170),
                ]), padding=ft.Padding.symmetric(horizontal=20, vertical=10), bgcolor="#F8FAF7"))
            children.extend(self.transfer_row(item) for item in visible)
        else:
            empty_title = "No matching transfers" if self.state.scanned else "A fresh start for unfinished transfers."
            empty_body = "Try a different filter or a wider history range. Results cover only the supported route and scanned blocks." if self.state.scanned else "Enter a public wallet address above. We'll reconstruct supported transfers and check what still needs to happen."
            children.append(ft.Container(ft.Column([
                ft.Container(ft.Icon(ft.Icons.ALT_ROUTE_ROUNDED, color=BLUE, size=32), width=76, height=76,
                             bgcolor="#EAF1E9", border_radius=24, alignment=ft.Alignment.CENTER),
                self.text(empty_title, 19, INK, ft.FontWeight.W_700, text_align=ft.TextAlign.CENTER),
                self.text(empty_body, 12, MUTED, max_width=390, text_align=ft.TextAlign.CENTER),
                ft.TextButton("Explore example transfers", on_click=self.examples, style=ft.ButtonStyle(color=BLUE)),
            ], spacing=15, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                alignment=ft.Alignment.CENTER, padding=ft.Padding.symmetric(horizontal=25, vertical=45)))
        if self.state.coverage:
            children.extend([ft.Divider(color=LINE, height=1), ft.Container(ft.Row([
                ft.Icon(ft.Icons.HISTORY_ROUNDED, size=13, color=MUTED),
                self.text(self.state.coverage, 10, MUTED, expand=True)], spacing=7), padding=14, bgcolor="#FAFBF9")])
        if self.state.partial:
            children.append(ft.Container(self.text("History is partial. Other transfers may exist outside this coverage.", 11, AMBER), padding=12))
        return ft.Column(children, spacing=0)

    def transfer_row(self, item):
        selected = self.state.selected and item["key"] == self.state.selected["key"]
        coin = ft.Container(self.text("$", 17, "#FFFFFF", ft.FontWeight.W_700), width=31, height=31,
                             bgcolor="#2775CA", border_radius=16, alignment=ft.Alignment.CENTER)
        identity = ft.Row([coin, ft.Column([
            self.text("USDC transfer", 12, INK, ft.FontWeight.W_700),
            self.text("Example · " + shorten(item["tx_hash"], 3) if item.get("example") else shorten(item["tx_hash"], 4), 10, MUTED),
        ], spacing=5)], spacing=11, expand=True)
        amount = ft.Column([self.text(display_amount(item["amount"]), 13, INK, ft.FontWeight.W_700),
                            self.text("USDC", 10, MUTED)], width=100, spacing=4)
        if self.narrow:
            row = ft.Column([ft.Row([identity, amount]), ft.Row([self.pill(item["status"]),
                             ft.TextButton("Inspect", on_click=lambda _: self.inspect(item), style=ft.ButtonStyle(color=BLUE))],
                             alignment=ft.MainAxisAlignment.SPACE_BETWEEN)], spacing=12)
        else:
            row = ft.Row([identity, amount, ft.Container(self.pill(item["status"]), width=150),
                ft.Icon(ft.Icons.CHEVRON_RIGHT_ROUNDED, color=BLUE if selected else "#9AA89D", size=18)], spacing=10)
        return ft.Container(row, padding=ft.Padding.symmetric(horizontal=17, vertical=18),
                            bgcolor="#EDF4ED" if selected else "#FFFFFF",
                            border=ft.Border(left=ft.BorderSide(3, BLUE if selected else "transparent"),
                                             bottom=ft.BorderSide(1, "#EAF0E8")),
                            on_click=lambda _: self.inspect(item), ink=True,
                            tooltip=f"Inspect {display_amount(item['amount'])} USDC")

    def recovery_note(self):
        steps = [("Source burn", "Find the original event on Base Sepolia."),
                 ("Circle attestation", "Match the signed message to that transfer."),
                 ("Destination receipt", "Check whether Arc already finished the mint.")]
        controls = [ft.Container(ft.Icon(ft.Icons.CALL_RECEIVED_ROUNDED, size=26, color=MINT),
                    width=49, height=49, bgcolor="#2B5546", border_radius=14, alignment=ft.Alignment.CENTER),
                    self.text("Every transfer\nhas a trail.", 25, "#FFFFFF", ft.FontWeight.W_700),
                    self.text("Follow the evidence, then take the right next step.", 12, "#BCD0C2")]
        for label, description in steps:
            controls.append(ft.Row([ft.Container(width=7, height=7, bgcolor=MINT, border_radius=4),
                ft.Column([self.text(label, 12, "#FFFFFF", ft.FontWeight.W_600),
                           self.text(description, 11, "#AAC4B2")], spacing=5, expand=True)], spacing=13,
                           vertical_alignment=ft.CrossAxisAlignment.START))
        controls.append(ft.TextButton("How recovery works", on_click=self.about,
                         style=ft.ButtonStyle(color=MINT, padding=0)))
        return ft.Container(ft.Column(controls, spacing=24), bgcolor=FOREST, padding=25, border_radius=14)

    def inspector(self):
        item = self.state.selected
        title, description, _, _ = state_copy(item["status"])
        header = ft.Container(ft.Column([
            ft.Row([self.text("Transfer details", 11, "#BED2C4", expand=True),
                    ft.IconButton(ft.Icons.CLOSE_ROUNDED, icon_color="#D9E8DF", icon_size=17, on_click=self.back, tooltip="Close details")]),
            self.text(display_amount(item["amount"]) + " USDC", 28, "#FFFFFF", ft.FontWeight.W_700),
            self.text("Base Sepolia  →  Arc Testnet", 11, "#BED2C4"),
            self.badge(title, FOREST, MINT),
        ], spacing=12), bgcolor=FOREST, padding=22)
        steps = [("Source burn", item["status"] not in {"source_changed", "unavailable", "source_confirming"}),
                 ("Circle attestation", item["status"] in {"ready", "completed", "caller_required", "expired", "submitted"}),
                 ("Destination mint", item["status"] == "completed")]
        path = ft.Column([ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED if done else ft.Icons.RADIO_BUTTON_UNCHECKED_ROUNDED,
            size=17, color=BLUE if done else "#B4C1B6"), self.text(label, 12, INK if done else MUTED)], spacing=10)
            for label, done in steps], spacing=16)
        values = [self.value_line("Fixed recipient", item["recipient"], selectable=True),
                  self.value_line("Recipient receives", display_amount(item.get("net_amount")) + " USDC" if item.get("net_amount") else "Waiting for attestation"),
                  self.value_line("Protocol fee", display_amount(item["fee"]) + " USDC" if item.get("fee") is not None else "Not available yet")]
        controls = [header, ft.Container(ft.Column([
            self.text(item.get("explanation") or description, 12, MUTED), path,
            ft.Divider(color=LINE), *values,
            self.text("The original recipient cannot be changed.", 10, MUTED),
        ], spacing=19), padding=22), self.next_step(item, description)]
        evidence = ft.ExpansionTile(title=self.text("Transfer evidence", 12, INK, ft.FontWeight.W_600),
            controls=[ft.Container(ft.Column([
                self.value_line("Source transaction", item["tx_hash"], selectable=True),
                self.value_line("Block / log", f"{item['block_number']:,} / {item['log_index']}"),
                self.value_line("Destination caller", "Any caller" if item.get("caller") == ZERO else item.get("caller", "Unknown"), selectable=True),
                self.value_line("Message nonce", item.get("nonce") or "Not available yet", selectable=True),
                ft.TextButton("Open source receipt", icon=ft.Icons.OPEN_IN_NEW_ROUNDED, url=item.get("source_url"),
                              disabled=self.state.example or not item.get("source_url")),
            ], spacing=16), padding=ft.Padding.symmetric(horizontal=18, vertical=12))],
            collapsed_bgcolor="#FAFBF9", bgcolor="#FFFFFF", tile_padding=ft.Padding.symmetric(horizontal=20, vertical=4))
        controls.append(evidence)
        if self.state.prepared:
            controls.append(self.transaction_review(self.state.prepared))
        return ft.Container(ft.Column(controls, spacing=0), bgcolor="#FFFFFF", border=ft.Border.all(1, LINE),
                            border_radius=13, clip_behavior=ft.ClipBehavior.ANTI_ALIAS)

    def search(self, _=None):
        self.state.query = self.search_input.value or ""
        selection_cleared = self.clear_hidden_selection()
        # Keep the search field mounted: replacing its ancestors on each keypress
        # loses Flutter keyboard focus and selection after the first character.
        self.ledger_host.content = self.ledger_content()
        self.ledger_host.update()
        if selection_cleared and (self.page.width or 1200) >= 1180:
            self.inspector_host.content = self.recovery_note()
            self.inspector_host.update()

    def clear_hidden_selection(self):
        selected = self.state.selected
        if selected and not any(item["key"] == selected["key"] for item in self.state.visible_transfers()):
            self.state.selected = None
            self.state.prepared = None
            self.review_error = ""
            return True
        return False

    def coverage(self, _=None):
        dialog = ft.AlertDialog(title=self.text("Supported route", 22, INK, ft.FontWeight.W_700),
            content=ft.Column([self.text("Native USDC · Base Sepolia → Arc Testnet", 14, INK, ft.FontWeight.W_600),
                self.text(self.state.coverage or "Scan a public address to inspect the exact source block range. The app covers supported CCTP V2 burns; it does not search every bridge or unknown router.", 13, MUTED),
                self.text("An empty result is not proof that no unfinished transfer exists outside this coverage.", 12, MUTED)],
                spacing=16, tight=True, width=440),
            actions=[ft.TextButton("Close", on_click=lambda _: self.page.pop_dialog())], bgcolor="#FFFFFF")
        self.page.show_dialog(dialog)

    def next_step(self, item, description):
        controls = [self.text("Next step", 17, INK, ft.FontWeight.W_600)]
        if self.state.example:
            controls.extend([self.text(description, 14, MUTED),
                ft.Container(ft.Row([ft.Icon(ft.Icons.SCIENCE_OUTLINED, color=AMBER, size=20),
                    self.text("This example cannot prepare or sign a transaction.", 13, AMBER, expand=True)]),
                    bgcolor="#FFF3DF", padding=14, border_radius=10)])
        elif item["status"] in {"ready", "caller_required"}:
            controls.extend([
                self.text("Review a destination mint using the address that will actually sign it.", 14, MUTED),
                self.caller_input,
                self.button("Checking recovery…" if self.preparing else "Review recovery", self.prepare,
                            primary=True, disabled=self.preparing, icon=ft.Icons.FACT_CHECK_OUTLINED),
                self.text("No signing happens in this page. Keep private keys out of your browser.", 12, MUTED),
            ])
            if self.review_error:
                controls.append(self.text(self.review_error, 13, "#AE3434"))
        else:
            controls.append(self.text(description, 14, MUTED))
            if item["status"] == "expired":
                controls.append(ft.TextButton("Circle re-attestation guide", url="https://developers.circle.com/cctp/howtos/troubleshoot-transfers", icon=ft.Icons.OPEN_IN_NEW_ROUNDED))
            controls.append(self.button("Refresh live state", self.scan, icon=ft.Icons.REFRESH_ROUNDED))
        return ft.Container(ft.Column(controls, spacing=16,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH), padding=22,
                            border=ft.Border(top=ft.BorderSide(1, LINE)))

    def value_line(self, label, value, selectable=False):
        return ft.Column([self.text(label, 12, MUTED), self.text(value, 14, INK,
                           ft.FontWeight.W_500, selectable=selectable)], spacing=5)

    def transaction_review(self, prepared):
        item = self.state.selected
        transaction = json.dumps(prepared["transaction"], indent=2)
        command = f"uv run python -m rebound.cli recover {item['tx_hash']} {item['log_index']} --caller {prepared['transaction']['from']}"
        async def copy(_):
            await self.clipboard.set(transaction)
        return self.panel(ft.Column([
            ft.Row([ft.Icon(ft.Icons.FACT_CHECK_OUTLINED, color=BLUE, size=24),
                    self.text("Unsigned recovery transaction", 21, INK, ft.FontWeight.W_600)], spacing=12),
            self.text("The source transfer, destination nonce and caller were rechecked, and the mint was simulated. This transaction has not been signed or sent.", 14, MUTED),
            self.value_line("Recipient", prepared.get("recipient", item["recipient"]), selectable=True),
            self.value_line("Destination amount", display_amount(prepared.get("net_amount")) + " USDC"),
            ft.TextField(value=transaction, read_only=True, multiline=True, min_lines=6, max_lines=10,
                         text_size=12, filled=True, fill_color="#F7F9FC", border_color=LINE),
            ft.Row([self.button("Copy transaction JSON", copy, icon=ft.Icons.CONTENT_COPY_ROUNDED)], wrap=True),
            self.text("To sign locally", 15, INK, ft.FontWeight.W_600),
            self.text(command, 12, INK, selectable=True),
            self.text("The local command revalidates the transfer again and asks for confirmation before sending on Arc Testnet. Never paste a private key into this app.", 13, MUTED),
            self.text(prepared.get("warning") or "A successful simulation does not guarantee later execution. Competing attempts can still consume gas.", 12, AMBER),
        ], spacing=16))

    def footer(self):
        note = ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=14, color=MUTED),
            self.text("Native USDC · CCTP V2 · Base Sepolia → Arc Testnet", 10, MUTED, expand=True)],
            expand=not self.narrow, spacing=8)
        link = ft.TextButton("How recovery works", on_click=self.about, style=ft.ButtonStyle(color=MUTED))
        content = ft.Column([note, link], spacing=8) if self.narrow else ft.Row([note, link], spacing=16)
        return ft.Container(content, padding=ft.Padding.only(bottom=20))

    async def scan(self, _=None) -> None:
        if self.state.busy:
            return
        self.state.busy = True
        self.render()
        await self.state.scan(self.service, self.wallet_input.value or self.state.wallet, int(self.history.value or "2000"))
        self.render()

    def examples(self, _=None) -> None:
        self.state.load_examples()
        self.render()

    def return_live(self, _=None) -> None:
        self.state = InboxState(wallet=self.state.wallet)
        self.render()

    def set_filter(self, key) -> None:
        self.state.filter = key
        self.clear_hidden_selection()
        self.render()

    def inspect(self, item) -> None:
        self.state.selected = item
        self.state.prepared = None
        self.caller_input.value = ""
        self.review_error = ""
        self.render()

    def back(self, _=None) -> None:
        self.state.selected = None
        self.state.prepared = None
        self.render()

    async def prepare(self, _=None) -> None:
        if self.preparing:
            return
        self.preparing = True
        self.review_error = ""
        self.render()
        try:
            await self.state.prepare(self.service, self.state.selected, self.caller_input.value or "")
        except Exception as exc:
            self.review_error = safe_error(exc)
        finally:
            self.preparing = False
            self.render()

    def about(self, _=None) -> None:
        dialog = ft.AlertDialog(
            title=self.text("Finish the original transfer", 24, INK, ft.FontWeight.W_600),
            content=ft.Column([
                self.text("Rebound discovers supported source burns, asks Circle for their signed messages, and checks whether Arc has already consumed each nonce.", 15, MUTED),
                self.text("An eligible recovery completes that same transfer to the recipient encoded in its message. It does not send USDC a second time or change the recipient.", 15, MUTED),
                self.text("Coverage is limited to the displayed route and history range. Unknown router contracts, unsupported messages and restricted callers can need a different recovery path.", 14, MUTED),
            ], spacing=16, width=500, tight=True),
            actions=[ft.TextButton("Got it", on_click=lambda _: self.page.pop_dialog())],
            bgcolor="#FFFFFF", scrollable=True,
        )
        self.page.show_dialog(dialog)


def build_main(service):
    """Return the async page entrypoint expected by flet.fastapi.app()."""
    async def main(page: ft.Page):
        InboxUI(page, service).install()
    return main
