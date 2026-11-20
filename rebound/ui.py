"""Python/Flutter transfer inbox; financial state is supplied by RecoveryService."""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import flet as ft

from rebound.clients import UpstreamError
from rebound.examples import EXAMPLE_WALLET, example_stream_transfer, example_transfers
from rebound.protocol import ZERO, address, hash32
from rebound.service import MIN_RECOVERY_CONFIRMATIONS

# Ledger palette. See docs/DESIGN.md: ink means settled, the one accent means "act here",
# amber means waiting and red means the record needs review.
PAPER = "#F3F4F2"
SURFACE = "#FFFFFF"
INK = "#141A17"
INK_2 = "#48514C"
INK_3 = "#68706B"
RULE = "#E1E4E0"
RULE_STRONG = "#C8CDC8"
ACCENT = "#1D5A44"
SELECTED = "#EDF1EE"
AMBER = "#9A5B0B"
AMBER_WASH = "#FBF4E8"
AMBER_RULE = "#EAD5B2"
RED = "#A8332B"
RED_WASH = "#FBEFEE"
SANS = "Manrope"
MONO = "PlexMono"
MONO_MEDIUM = "PlexMonoMedium"
TONES = {"action": ACCENT, "ok": INK, "wait": AMBER, "stop": RED, "idle": INK_3}

# Controlled testnet recovery documented in PR #3. Status is checked live.
LIVE_SAMPLE_WALLET = "0x395809d8da4a70145d5060448070a15367df722c"
LIVE_SAMPLE_END_BLOCK = 61_175_721
LIVE_SAMPLE_BLOCKS = 200
CASE_PATH = re.compile(r"/transfer/(0x[0-9a-fA-F]{64})/([0-9]{1,8})/?", re.ASCII)

# The inspector rechecks one selected live transfer while it waits on Base or Circle.
RECHECK_STATUSES = frozenset({"source_confirming", "awaiting_attestation"})
RECHECK_SECONDS = 10
RECHECK_LIMIT_SECONDS = 600
UNFINISHED_STATUSES = frozenset({"ready", "awaiting_attestation", "source_confirming", "caller_required"})

STATE_COPY = {
    "ready": ("Ready to finish", "A signed message is available. Rebound will recheck it before preparing the destination mint.", "action"),
    "awaiting_attestation": ("Waiting for Circle", "Your source transfer was found. Wait for Circle's attestation, then scan again.", "wait"),
    "source_confirming": ("Source confirming", "The source transfer needs more confirmations before recovery can be prepared.", "wait"),
    "completed": ("Completed", "The destination has already consumed this message's nonce. Do not send the transfer again.", "ok"),
    "caller_required": ("Caller required", "Only the destination caller named in the original transfer can deliver this message. The recipient cannot change that rule.", "wait"),
    "expired": ("Attestation expired", "The original burn still exists. It needs a fresh Circle attestation before it can be completed.", "wait"),
    "unsupported": ("Outside current support", "Rebound cannot safely prepare this transfer with the current route and message support.", "idle"),
    "managed_delivery": ("Forwarded transfer", "Check delivery in the original app and refresh the Arc status here.", "wait"),
    "unavailable": ("Could not verify", "A network or data source is unavailable. Try again before taking any action.", "idle"),
    "source_changed": ("Source needs review", "The source receipt no longer matches the indexed event. Rebound will not prepare a recovery.", "stop"),
    "submitted": ("Mint submitted", "A destination transaction was submitted. Check its receipt before attempting another mint.", "wait"),
}


def state_copy(status: str) -> tuple:
    return STATE_COPY.get(status, STATE_COPY["unavailable"])


def display_amount(value: str | None) -> str:
    if value is None:
        return "Not available yet"
    whole, _, fraction = f"{Decimal(value):,f}".partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def aligned_amounts(*values: str | None) -> list[str | None]:
    """Give related USDC amounts one shared precision so they align on the decimal point."""
    # Read the exact decimal strings; Decimal.normalize() rounds large USDC coefficients.
    places = max([2] + [len(value.partition(".")[2].rstrip("0"))
                        for value in values if value is not None])
    places = min(places, 6)
    return [None if value is None else f"{Decimal(value):,.{places}f}" for value in values]


def shorten(value: str | None, size: int = 6) -> str:
    if not value:
        return "Not available yet"
    return value if len(value) < size * 2 + 4 else f"{value[:size + 2]}…{value[-size:]}"


def safe_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, UpstreamError)):
        return str(exc)
    return "The transfer could not be verified. Try again or use a narrower history range."


def chain_time(value: object) -> datetime | None:
    """Read a block timestamp supplied by RecoveryService. Never substitute a local time."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else None


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def format_clock(moment: datetime, now: datetime) -> str:
    """UTC time of day, with the date only when it is not today's date."""
    return moment.strftime("%H:%M:%S UTC" if moment.date() == now.date() else "%Y-%m-%d %H:%M:%S UTC")


def burn_time(item: dict, now: datetime) -> str | None:
    burned = chain_time(item.get("burned_at"))
    if burned is None:
        return None
    return f"Burned {format_clock(burned, now)}, {format_duration((now - burned).total_seconds())} ago"


def mint_time(item: dict, now: datetime) -> str | None:
    minted = chain_time(item.get("minted_at"))
    if minted is None:
        return None
    burned = chain_time(item.get("burned_at"))
    after = (f", {format_duration((minted - burned).total_seconds())} after the burn"
             if burned is not None and minted >= burned else "")
    return f"Minted {format_clock(minted, now)}{after}"


def unfinished_for(item: dict, now: datetime) -> str | None:
    burned = chain_time(item.get("burned_at"))
    if burned is None or item.get("status") not in UNFINISHED_STATUSES:
        return None
    return f"Unfinished for {format_duration((now - burned).total_seconds())}"


def proof_steps(item: dict) -> list[tuple[str, str, str]]:
    """Burn, attestation and mint states as (title, state, tone), derived from verified fields."""
    status = item["status"]
    example = bool(item.get("example"))
    verified = (status not in {"source_changed", "unavailable", "source_confirming"} if example
                else bool(item.get("source_verified")))
    matched = (status in {"ready", "completed", "caller_required", "expired", "submitted"} if example
               else bool(item.get("attestation_matched")))
    if status == "source_changed":
        burn = ("Receipt changed", "stop")
    elif status == "source_confirming":
        burn = ("Confirming", "wait")
    elif verified:
        burn = ("Verified", "ok")
    elif status == "unavailable":
        burn = ("Unavailable", "wait")
    else:
        burn = ("Not verified", "idle")
    if status == "expired":
        attestation = ("Expired", "wait")
    elif matched:
        attestation = ("Matched", "ok")
    elif status == "awaiting_attestation":
        attestation = ("Waiting", "wait")
    elif status == "unsupported":
        attestation = ("No safe match", "stop")
    elif status == "unavailable":
        attestation = ("Unavailable", "wait")
    else:
        attestation = ("Not requested", "idle")
    if status == "completed" or item.get("nonce_used") is True:
        mint = ("Minted", "ok")
    elif status == "ready":
        mint = ("Not minted", "action")
    elif status == "submitted":
        mint = ("Submitted", "wait")
    elif status == "caller_required":
        mint = ("Caller restricted", "wait")
    elif status == "managed_delivery":
        mint = ("Forwarding", "wait")
    elif status == "expired":
        mint = ("Not minted", "wait")
    else:
        mint = ("Not checked", "idle")
    return [("Burn on Base Sepolia", *burn), ("Circle attestation", *attestation), ("Mint on Arc Testnet", *mint)]


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
    mode: str = "wallet"

    def load_examples(self, *, stream: bool = False) -> None:
        self.transfers = [example_stream_transfer()] if stream else example_transfers()
        self.example = True
        self.mode = "stream" if stream else "wallet"
        self.scanned = True
        self.partial = False
        self.filter = "all"
        self.query = ""
        self.error = ""
        self.coverage = ("Simulated Stream delivery. This case uses synthetic data."
                         if stream else "Four synthetic transfers show how recovery states work. No live funds are represented.")
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

    async def scan(self, service: Any, search: str, blocks: int, end_block: str = "", *,
                   mode: str = "wallet", log_index: int | None = None) -> None:
        self.busy = True
        self.example = False
        self.scanned = False
        self.mode = mode
        self.error = ""
        self.coverage = ""
        self.partial = False
        self.transfers = []
        self.selected = None
        self.prepared = None
        self.filter = "all"
        self.query = ""
        try:
            if mode == "transaction":
                options = {"log_index": log_index} if log_index is not None else {}
                result = await service.inspect_transaction(hash32(search.strip()), **options)
            else:
                wallet = address(search.strip())
                if wallet == EXAMPLE_WALLET:
                    self.load_examples(stream=(mode == "stream"))
                    return
                self.wallet = wallet
                if mode == "stream":
                    result = await service.scan_stream(wallet)
                else:
                    anchor = end_block.strip()
                    if anchor and (not anchor.isascii() or not anchor.isdecimal() or len(anchor) > 16):
                        raise ValueError("Enter a Base Sepolia end block using digits only.")
                    options = {"end_block": int(anchor)} if anchor else {}
                    result = await service.scan(wallet, blocks=blocks, **options)
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
        return await service.prepare(transfer["tx_hash"], transfer["log_index"], signer)


@dataclass(eq=False)
class Recheck:
    """One automatic recheck run, bound to one selected live transfer."""
    state: InboxState
    key: str
    started: float
    remaining: int = RECHECK_SECONDS
    active: bool = True
    note: str = ""


def field_style(**overrides) -> dict:
    style = dict(filled=True, fill_color=SURFACE, border_color=RULE_STRONG, focused_border_color=ACCENT,
                 border_radius=6, cursor_color=ACCENT, autocorrect=False,
                 text_style=ft.TextStyle(size=13, font_family=MONO, color=INK),
                 hint_style=ft.TextStyle(size=13, font_family=SANS, color=INK_3),
                 content_padding=ft.Padding.symmetric(horizontal=12, vertical=12))
    return style | overrides


class InboxUI:
    def __init__(self, page: ft.Page, service: Any):
        self.page = page
        self.service = service
        self.state = InboxState()
        self.wallet_input = ft.TextField(hint_text="Wallet address, 0x…", height=48,
                                         on_submit=self.scan, **field_style())
        self.tx_input = ft.TextField(hint_text="Base Sepolia transaction hash, 0x…", height=48,
                                     on_submit=self.inspect_transaction, **field_style())
        self.show_transaction = False
        self.history = ft.Dropdown(
            value="2000", width=176, text_size=13, dense=True,
            options=[ft.DropdownOption("200", "200 blocks"),
                     ft.DropdownOption("2000", "2,000 blocks"),
                     ft.DropdownOption("10000", "10,000 blocks"),
                     ft.DropdownOption("50000", "50,000 blocks")],
            filled=True, fill_color=SURFACE, border_color=RULE_STRONG, focused_border_color=ACCENT,
            border_radius=6, content_padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        )
        self.end_block = ft.TextField(hint_text="End block, blank for latest", width=230, height=40,
                                      keyboard_type=ft.KeyboardType.NUMBER, on_submit=self.scan,
                                      **field_style(content_padding=ft.Padding.symmetric(horizontal=12, vertical=10)))
        self.show_history = False
        self.caller_input = ft.TextField(hint_text="Signing address, 0x…", height=42,
                                         on_change=self.caller_changed, **field_style())
        self.receipt_hash_input = ft.TextField(hint_text="Arc transaction hash, 0x…", height=42,
                                               on_submit=self.verify_arc_receipt, **field_style())
        self.receipt_error = ""
        self._receipt_request = None
        self.review_error = ""
        self.preparing = False
        self.refreshing = False
        self.refresh_error = ""
        self.replaying = False
        self.replay_item = None
        self.replay_result = None
        self.replay_error = ""
        self._recheck: Recheck | None = None
        self._recheck_text: ft.Text | None = None
        self.client_connected = True
        self.recheck_sleep = asyncio.sleep
        self.recheck_clock = time.monotonic
        self.root = ft.Row(expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        self.search_input = ft.TextField(hint_text="Filter by hash, address or amount", height=34, width=250,
            on_change=self.search,
            **field_style(text_style=ft.TextStyle(size=12, font_family=MONO, color=INK),
                          hint_style=ft.TextStyle(size=12, font_family=SANS, color=INK_3),
                          content_padding=ft.Padding.symmetric(horizontal=10, vertical=8)))
        self.ledger_host = ft.Container()
        self.inspector_host = ft.Container()
        self.inspector_scroll = ft.Column([self.inspector_host], scroll=ft.ScrollMode.AUTO, expand=True,
                                         horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.workspace_view = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True,
                                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.split_body = ft.Row([
            ft.Container(self.workspace_view, expand=True, padding=ft.Padding.only(left=28, right=28, top=22)),
            ft.Container(self.inspector_scroll, width=420, bgcolor=SURFACE,
                         border=ft.Border(left=ft.BorderSide(1, RULE))),
        ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
        self.header_host = ft.Container()
        self.body_host = ft.Container(expand=True)
        self.main = ft.Column([self.header_host, self.body_host], spacing=0, expand=True,
                              horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.review_host = ft.Container(visible=False)
        self.clipboard = ft.Clipboard()
        self.semantics = ft.SemanticsService()
        self.motion_enabled = False
        self._revealing_item = None
        self._installed = False
        self._active_route = None

    def install(self) -> None:
        self.page.title = "Rebound"
        self.page.bgcolor = PAPER
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.fonts = {SANS: "/fonts/Manrope.ttf", MONO: "/fonts/IBMPlexMono-Regular.ttf",
                           MONO_MEDIUM: "/fonts/IBMPlexMono-Medium.ttf"}
        self.page.theme = ft.Theme(
            font_family=SANS, use_material3=True, visual_density=ft.VisualDensity.COMPACT,
            color_scheme=ft.ColorScheme(primary=ACCENT, on_primary=SURFACE, surface=SURFACE, on_surface=INK,
                                        on_surface_variant=INK_2, outline=RULE_STRONG, outline_variant=RULE,
                                        surface_tint=SURFACE, error=RED),
            splash_color="#00000000", divider_color=RULE)
        self.page.services.append(self.clipboard)
        self.page.services.append(self.semantics)
        self.page.on_resize = self.resize
        self.page.on_route_change = self.route_changed
        # A disconnected tab skips its rechecks until Flet reconnects the session.
        self.page.on_connect = lambda _: setattr(self, "client_connected", True)
        self.page.on_disconnect = lambda _: setattr(self, "client_connected", False)
        self.page.add(self.root)
        self._installed = True
        self.render()

    async def load_motion_preference(self) -> None:
        try:
            async with asyncio.timeout(1):
                features = await self.semantics.get_accessibility_features()
        except (RuntimeError, ValueError, TimeoutError):
            return
        self.motion_enabled = not (features.disable_animations or features.reduce_motion)

    def resize(self, _=None) -> None:
        self.render()

    @property
    def narrow(self) -> bool:
        return (self.page.width or 1100) < 720

    @property
    def split_workspace(self) -> bool:
        return (self.page.width or 1100) >= 1080

    @property
    def ledger_width(self) -> float:
        width = self.page.width or 1100
        return width - 476 if self.split_workspace else width - (32 if self.narrow else 48)

    # Type: Manrope carries words, IBM Plex Mono carries every hash, address, block and amount.
    def text(self, value, size=13, color=INK, weight=None, *, mono=False, **kwargs):
        family = (MONO_MEDIUM if weight in {ft.FontWeight.W_500, ft.FontWeight.W_600, ft.FontWeight.W_700}
                  else MONO) if mono else None
        return ft.Text(str(value), size=size, color=color, weight=None if mono else weight,
                       font_family=family, **kwargs)

    def button(self, label, callback=None, *, primary=False, icon=None, disabled=False, url=None, height=40):
        style = ft.ButtonStyle(
            bgcolor={ft.ControlState.DISABLED: RULE, ft.ControlState.DEFAULT: ACCENT} if primary else SURFACE,
            color={ft.ControlState.DISABLED: INK_3, ft.ControlState.DEFAULT: SURFACE if primary else INK},
            side=None if primary else ft.BorderSide(1, RULE_STRONG),
            shape=ft.RoundedRectangleBorder(radius=6), elevation=0,
            padding=ft.Padding.symmetric(horizontal=16, vertical=0),
            text_style=ft.TextStyle(size=13, weight=ft.FontWeight.W_600, font_family=SANS),
            overlay_color="#14FFFFFF" if primary else "#0A141A17")
        cls = ft.FilledButton if primary else ft.OutlinedButton
        return cls(label, icon=icon, on_click=callback, disabled=disabled, url=url, style=style, height=height)

    def link(self, label, callback=None, *, url=None, disabled=False, color=ACCENT, icon=None, flush=False):
        return ft.TextButton(label, icon=icon, icon_color=color, on_click=callback, url=url, disabled=disabled,
                             height=30, style=ft.ButtonStyle(
                                 color={ft.ControlState.DISABLED: INK_3, ft.ControlState.DEFAULT: color},
                                 padding=ft.Padding.symmetric(horizontal=0 if flush else 6, vertical=0), icon_size=14,
                                 shape=ft.RoundedRectangleBorder(radius=4),
                                 text_style=ft.TextStyle(size=13, weight=ft.FontWeight.W_600, font_family=SANS)))

    def status_mark(self, status, *, size=13, **kwargs):
        title, _, tone = state_copy(status)
        return ft.Row([ft.Container(width=7, height=7, border_radius=4, bgcolor=TONES[tone]),
                       self.text(title, size, INK, ft.FontWeight.W_700 if tone == "action" else ft.FontWeight.W_500)],
                      spacing=8, tight=True, **kwargs)

    def section_title(self, label, trailing=None):
        controls = [self.text(label, 13, INK, ft.FontWeight.W_700, expand=True)]
        if trailing is not None:
            controls.append(trailing)
        return ft.Row(controls, spacing=8)

    def render(self) -> None:
        self.wallet_input.disabled = self.state.busy
        self.tx_input.disabled = self.state.busy
        self.history.disabled = self.state.busy
        self.end_block.disabled = self.state.busy
        self.caller_input.disabled = self.preparing
        self.search_input.value = self.state.query
        self.inspector_host.animate_opacity = None
        self.inspector_host.opacity = 0.88 if self._revealing_item is not None and self._revealing_item is self.state.selected else 1
        content = [self.example_banner()] if self.state.example else []
        sheet = False
        if self.split_workspace:
            self.inspector_host.content = self.inspector() if self.state.selected else self.inspector_placeholder()
            content.extend([self.scan_panel(), self.inbox()])
        elif self.state.selected:
            sheet = True
            self.inspector_host.content = self.inspector()
            back = ft.Container(ft.Row([self.link("All transfers", self.back, icon=ft.Icons.ARROW_BACK, color=INK_2)]),
                                bgcolor=SURFACE, border=ft.Border(bottom=ft.BorderSide(1, RULE)),
                                padding=ft.Padding.only(left=10, top=6, bottom=6))
            content = [ft.Container(control, padding=ft.Padding.only(left=16, right=16, top=12, bottom=12))
                       for control in content]
            content.extend([back, self.inspector_host])
        else:
            content.extend([self.scan_panel(), self.inbox()])
        self.workspace_view.controls = content
        self.workspace_view.spacing = 0 if sheet else 16
        self.body_host.content = self.split_body if self.split_workspace else self.workspace_view
        self.body_host.padding = 0 if self.split_workspace or sheet else (
            ft.Padding.only(left=16, right=16, top=16) if self.narrow else ft.Padding.only(left=24, right=24, top=22))
        self.header_host.content = self.header()
        self.root.controls = [self.main]
        self.page.update()

    def header(self):
        brand = self.text("Rebound", 16, INK, ft.FontWeight.W_800)
        route = self.text("Base Sepolia → Arc Testnet", 13, INK_2, ft.FontWeight.W_500)
        environment = ft.Container(self.text("Testnet", 11, INK_2, ft.FontWeight.W_700),
                                   border=ft.Border.all(1, RULE_STRONG), border_radius=3,
                                   padding=ft.Padding.symmetric(horizontal=6, vertical=1))
        links = [self.link("Coverage", self.coverage, color=INK_2), self.link("How it works", self.about, color=INK_2)]
        if self.narrow:
            content = ft.Column([ft.Row([brand, environment, ft.Container(expand=True), *links], spacing=8),
                                 route], spacing=2)
            padding = ft.Padding.only(left=16, right=8, top=10, bottom=10)
        else:
            content = ft.Row([brand, ft.Container(width=1, height=16, bgcolor=RULE_STRONG), route, environment,
                              ft.Container(expand=True), *links], spacing=14)
            padding = ft.Padding.only(left=28, right=20, top=12, bottom=12)
        return ft.Container(content, bgcolor=SURFACE, border=ft.Border(bottom=ft.BorderSide(1, RULE)),
                            padding=padding)

    def example_banner(self):
        return ft.Container(ft.Row([
            self.text("Example mode. These transfers are synthetic and nothing here can be signed.",
                      13, AMBER, ft.FontWeight.W_600, expand=True),
            self.link("Return to live", self.return_live, color=AMBER),
        ], spacing=12), bgcolor=AMBER_WASH, border=ft.Border.all(1, AMBER_RULE), border_radius=6,
            padding=ft.Padding.only(left=14, right=6, top=6, bottom=6))

    def scan_panel(self):
        busy = self.state.busy
        if self.show_transaction:
            entry = self.tx_input
            action = self.button("Checking…" if busy else "Check transaction", self.inspect_transaction,
                                 primary=True, disabled=busy, height=48)
            extras = []
        else:
            entry = self.wallet_input
            action = self.button("Scanning…" if busy else "Find transfers", self.scan, primary=True, disabled=busy,
                                 height=48)
            extras = [self.history]
        if self.narrow:
            entry.expand = False
            self.history.expand = True
            self.history.width = None
            form = ft.Column([entry, ft.Row([*extras, action], spacing=8)], spacing=8,
                             horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        else:
            entry.expand = True
            self.history.expand = False
            self.history.width = 176
            form = ft.Row([entry, *extras, action], spacing=8)
        options = [self.link("Search by wallet" if self.show_transaction else "Search by transaction",
                             self.toggle_transaction, disabled=busy)]
        if not self.show_transaction:
            options.extend(self.history_options())
            if self.state.example:
                options.extend([self.link("Copy demo wallet", self.copy_demo_wallet),
                                self.link("Show simulated Stream delivery", self.example_stream, disabled=busy)])
            elif self.state.scanned or self.state.error:
                # Before the first lookup these two actions live in the empty ledger instead.
                options.extend([self.link("Check a live sample", self.live_sample, disabled=busy),
                                self.link("Explore examples", self.examples, disabled=busy)])
        controls = [form, ft.Row(options, wrap=True, spacing=4, run_spacing=0)]
        if self.show_history and not self.show_transaction:
            controls.append(ft.Row([self.end_block,
                self.text("Scan back from this Base Sepolia block to revisit an older window.", 12, INK_3)],
                wrap=True, spacing=12))
        if busy:
            controls.extend([ft.ProgressBar(color=ACCENT, bgcolor=RULE, bar_height=2),
                             self.text("Reading source events, Circle attestations and destination state…", 12, INK_2)])
        if self.state.error:
            controls.append(ft.Container(self.text(self.state.error, 13, RED, ft.FontWeight.W_600),
                                         bgcolor=RED_WASH, border=ft.Border.all(1, "#EFCFCB"), border_radius=6,
                                         padding=ft.Padding.symmetric(horizontal=12, vertical=9)))
        return ft.Column(controls, spacing=8)

    def history_options(self):
        history_label = "Hide history options" if self.show_history else "History options"
        if not self.show_history and self.end_block.value.strip():
            history_label = f"History options (end block {self.end_block.value.strip()[:16]})"
        options = [self.link(history_label, self.toggle_history, disabled=self.state.busy)]
        if not self.state.example and getattr(getattr(self.service, "settings", None), "stream_enabled", False):
            options.append(self.link("Search Stream index", self.scan_index, disabled=self.state.busy))
        return options

    def inbox(self):
        counts = self.state.counts()

        def tab(label, key):
            active = self.state.filter == key
            parts = [self.text(label, 13, INK if active else INK_2, ft.FontWeight.W_700 if active else ft.FontWeight.W_500)]
            if self.state.scanned:
                parts.append(self.text(counts[key], 12, INK if active else INK_3, mono=True))
            return ft.Container(ft.Row(parts, spacing=6, tight=True),
                                padding=ft.Padding.only(top=12, bottom=10),
                                border=ft.Border(bottom=ft.BorderSide(2, INK if active else "#00000000")),
                                on_click=lambda _: self.set_filter(key))
        tabs = ft.Row([tab("All" if self.narrow else "All transfers", "all"), tab("Needs attention", "attention"),
                       tab("Completed", "completed")],
                      spacing=18, tight=True, scroll=ft.ScrollMode.HIDDEN if self.narrow else None)
        if self.narrow or self.ledger_width < 640:
            self.search_input.width = None
            self.search_input.expand = False
            top = ft.Column([tabs, ft.Container(self.search_input, padding=ft.Padding.only(bottom=10))], spacing=6,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        else:
            self.search_input.width = 250
            top = ft.Row([tabs, ft.Container(expand=True), self.search_input])
        self.ledger_host.content = self.ledger_content()
        # Tabs and the filter only mean something once a lookup has produced records.
        chrome = [ft.Container(top, padding=ft.Padding.symmetric(horizontal=16)),
                  ft.Divider(color=RULE, height=1, thickness=1)] if self.state.scanned else []
        return ft.Container(ft.Column([*chrome, self.ledger_host], spacing=0),
            bgcolor=SURFACE, border_radius=8, border=ft.Border.all(1, RULE), clip_behavior=ft.ClipBehavior.ANTI_ALIAS)

    def columns(self) -> list[tuple[str, int | None, bool]]:
        columns = [("Amount, USDC", 112, True), ("Status", 168, False), ("Source transaction", None, False),
                   ("Source block", 104, True)]
        if self.ledger_width >= 820:
            columns.append(("Recipient", 124, False))
        return columns

    def ledger_content(self):
        children = []
        visible = self.state.visible_transfers()
        if visible:
            if not self.narrow:
                cells = [self.text(label, 12, INK_3, ft.FontWeight.W_600, width=width, expand=width is None,
                                   text_align=ft.TextAlign.RIGHT if right else None)
                         for label, width, right in self.columns()]
                children.append(ft.Container(ft.Row(cells, spacing=20),
                                             padding=ft.Padding.symmetric(horizontal=16, vertical=9),
                                             border=ft.Border(bottom=ft.BorderSide(1, RULE))))
            children.extend(self.transfer_row(item) for item in visible)
        else:
            children.append(self.empty_ledger())
        if self.state.coverage:
            children.append(ft.Container(self.text(self.state.coverage, 12, INK_3),
                                         padding=ft.Padding.symmetric(horizontal=16, vertical=11)))
        if self.state.partial:
            children.append(ft.Container(self.text("History is partial. Other transfers may exist outside this coverage.",
                                                   12, AMBER, ft.FontWeight.W_600),
                                         padding=ft.Padding.only(left=16, right=16, bottom=11)))
        return ft.Column(children, spacing=0)

    def empty_ledger(self):
        if self.state.scanned:
            message = "No transfers match this filter." if self.state.transfers else {
                "stream": "The Stream index has no supported transfers for this address. Scan source history directly.",
                "transaction": "No supported burn in this transaction. Use the original Base Sepolia burn transaction hash.",
                "wallet": "No supported transfers in this range. Try a wider history range.",
            }[self.state.mode]
            return ft.Container(self.text(message, 13, INK_2), padding=ft.Padding.symmetric(horizontal=16, vertical=20))
        if self.state.busy:
            return ft.Container(self.text("Looking up transfers…", 13, INK_3),
                                padding=ft.Padding.symmetric(horizontal=16, vertical=20))
        if self.show_transaction:
            return ft.Container(self.text("Paste the Base Sepolia hash of the original burn transaction.", 13, INK_2),
                                padding=ft.Padding.symmetric(horizontal=16, vertical=20))
        return ft.Container(ft.Column([
            self.text("Enter a wallet address to list its USDC transfers from Base Sepolia to Arc.", 14, INK,
                      ft.FontWeight.W_600),
            ft.Row([self.button("Check a live sample", self.live_sample, disabled=self.state.busy),
                    self.link("Explore examples", self.examples, disabled=self.state.busy)], spacing=12, wrap=True),
        ], spacing=12), padding=ft.Padding.symmetric(horizontal=16, vertical=20))

    def transfer_row(self, item):
        selected = bool(self.state.selected and item["key"] == self.state.selected["key"])
        source = shorten(item["tx_hash"], 4)
        hash_cell = self.text(source, 12.5, INK_2, mono=True)
        block = f"{item['block_number']:,}" if item.get("block_number") is not None else "Not available"
        if self.narrow:
            row = ft.Column([
                ft.Row([ft.Row([self.text(display_amount(item["amount"]), 15, INK, ft.FontWeight.W_600, mono=True),
                                self.text("USDC", 11, INK_3, ft.FontWeight.W_600)], spacing=6, tight=True),
                        ft.Container(expand=True), self.status_mark(item["status"])]),
                ft.Row([hash_cell, ft.Container(expand=True), self.text(f"Block {block}", 12, INK_3, mono=True)]),
            ], spacing=6)
            padding = ft.Padding.symmetric(horizontal=16, vertical=12)
        else:
            cells = {"Amount, USDC": self.text(display_amount(item["amount"]), 13, INK, ft.FontWeight.W_500,
                                               mono=True, text_align=ft.TextAlign.RIGHT),
                     "Status": self.status_mark(item["status"]),
                     "Source transaction": hash_cell,
                     "Source block": self.text(block, 12.5, INK_2, mono=True, text_align=ft.TextAlign.RIGHT),
                     "Recipient": self.text(shorten(item.get("recipient"), 4), 12.5, INK_2, mono=True)}
            row = ft.Row([ft.Container(cells[label], width=width, expand=width is None,
                                       alignment=ft.Alignment.CENTER_RIGHT if right else ft.Alignment.CENTER_LEFT)
                          for label, width, right in self.columns()], spacing=20)
            padding = ft.Padding.symmetric(horizontal=16, vertical=11)
        return ft.Container(row, padding=padding, bgcolor=SELECTED if selected else SURFACE,
                            border=ft.Border(bottom=ft.BorderSide(1, RULE)),
                            on_click=lambda _: self.inspect(item))

    def proof_step(self, number, title, state, tone, details, *, last=False):
        color = TONES[tone]
        done = tone == "ok"
        marker = ft.Container(self.text(number, 11, SURFACE if done else color, ft.FontWeight.W_600, mono=True),
                              width=20, height=20, border_radius=10, alignment=ft.Alignment.CENTER,
                              bgcolor=INK if done else SURFACE, border=None if done else ft.Border.all(1, color))
        rail = [marker] if last else [marker, ft.Container(width=1, bgcolor=RULE, expand=True)]
        body = ft.Column([ft.Row([self.text(title, 13, INK, ft.FontWeight.W_600, expand=True),
                                  self.text(state, 12, color, ft.FontWeight.W_700)]), *details],
                         spacing=3, expand=True)
        return ft.Row([ft.Column(rail, spacing=4, width=20, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                       ft.Container(body, expand=True, padding=ft.Padding.only(bottom=0 if last else 16))],
                      spacing=12, intrinsic_height=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)

    def proof(self, item, now: datetime | None = None):
        live = not self.state.example and not item.get("example")
        now = now or datetime.now(UTC)

        def line(value):
            return self.text(value, 12, INK_2, mono=True, selectable=True)
        burned = burn_time(item, now)
        burn = [line(burned)] if burned else []
        burn.extend([line(f"Block {item['block_number']:,}, log {item['log_index']}"),
                     line(f"Tx {shorten(item['tx_hash'], 6)}")])
        confirmations = item.get("source_confirmations")
        if confirmations is not None:
            burn.append(line(f"{confirmations} of {MIN_RECOVERY_CONFIRMATIONS} Base confirmations"
                             if item["status"] == "source_confirming" else f"{confirmations:,} confirmations"))
        if live and item.get("source_url"):
            burn.append(self.proof_link("Open source receipt", item["source_url"]))
        attestation = []
        if item.get("nonce"):
            attestation.append(line(f"Nonce {shorten(item['nonce'], 6)}"))
        if item.get("expiration_block"):
            attestation.append(line(f"Expires at Arc block {item['expiration_block']:,}"))
        mint = []
        destination_url = item.get("destination_url") if live else None
        minted = mint_time(item, now) if destination_url else None
        if minted:
            mint.append(line(minted))
        if item.get("destination_block") is not None:
            # destination_block is when Rebound read the nonce, not when the mint happened.
            nonce_status = "used" if item["nonce_used"] else "unused"
            mint.append(line(f"Nonce {nonce_status}, read at Arc block {item['destination_block']:,}"))
        if destination_url:
            mint.append(self.proof_link("Open verified Arc receipt", destination_url))
        submitted_url = self.submitted_receipt_url(item)
        if submitted_url:
            mint.append(line("Submitted receipt matches the original message"))
            mint.append(self.proof_link("Open submitted Arc receipt", submitted_url))
        steps = proof_steps(item)
        details = [burn, attestation, mint]
        controls = [self.proof_step(index + 1, title, state, tone, details[index], last=index == 2)
                    for index, (title, state, tone) in enumerate(steps)]
        if not live:
            controls.insert(0, ft.Container(self.text(
                "SIMULATED PROOF · Example wallet",
                12, AMBER, ft.FontWeight.W_700), bgcolor=AMBER_WASH, padding=12,
                margin=ft.Margin.only(bottom=16)))
        return ft.Column(controls, spacing=0)

    def proof_link(self, label, url):
        return ft.Row([self.link(label, url=url, icon=ft.Icons.OPEN_IN_NEW, flush=True)])

    def inspector_placeholder(self):
        steps = [("Burn on Base Sepolia", "The source receipt holds the burn event, block and amount."),
                 ("Circle attestation", "Circle's signed message must match that burn exactly."),
                 ("Mint on Arc Testnet", "Arc's used-nonce record shows whether the mint already happened.")]
        rows = [self.proof_step(index + 1, title, "", "idle", [self.text(note, 12, INK_3)], last=index == 2)
                for index, (title, note) in enumerate(steps)]
        return ft.Container(ft.Column([
            self.text("Select a transfer to see its proof.", 15, INK, ft.FontWeight.W_700),
            self.text("Rebound checks three records for every transfer before it offers an action.", 13, INK_2),
            ft.Container(height=4), *rows,
        ], spacing=10), padding=24)

    def fact(self, label, value, *, mono=None):
        # Machine values (hashes, addresses, block numbers, amounts) are set in mono; words are not.
        mono = str(value)[:1].isdigit() if mono is None else mono
        value_text = self.text(value, 12.5, INK, mono=mono, selectable=True)
        if self.narrow or len(str(value)) > 30:
            return ft.Column([self.text(label, 12, INK_3), value_text], spacing=2)
        return ft.Row([self.text(label, 12, INK_3, width=128), ft.Container(value_text, expand=True)],
                      vertical_alignment=ft.CrossAxisAlignment.START)

    def amounts(self, item):
        managed = bool(item.get("delivery_mode"))
        downstream = item.get("delivery_mode") == "forwarding_with_app_hook"
        burned, fee, net = aligned_amounts(item["amount"], item.get("fee"), item.get("net_amount"))
        rows = [("Burned on Base Sepolia", burned, False),
                ("Attested fee" if managed else "Circle fee", fee or "Waiting for attestation", False),
                ("CCTP mint amount" if downstream else "Recipient receives", net or "Waiting for attestation", True)]
        lines = []
        for index, (label, value, total) in enumerate(rows):
            weight = ft.FontWeight.W_700 if total else None
            lines.append(ft.Container(ft.Row([
                self.text(label, 13, INK if total else INK_2, weight, expand=True),
                self.text(value, 13, INK, ft.FontWeight.W_600 if total else None, mono=value[0].isdigit()),
            ]), padding=ft.Padding.symmetric(vertical=7),
                border=ft.Border(top=ft.BorderSide(1, INK if total else RULE)) if index else None))
        return ft.Column([self.section_title("Amount", self.text("USDC", 12, INK_3)), ft.Column(lines, spacing=0)],
                         spacing=4)

    def inspector(self):
        item = self.state.selected
        title, description, tone = state_copy(item["status"])
        downstream = item.get("delivery_mode") == "forwarding_with_app_hook"
        head = [ft.Row([self.text(display_amount(item["amount"]), 30, INK, ft.FontWeight.W_600, mono=True),
                        self.text("USDC", 13, INK_3, ft.FontWeight.W_700)],
                       spacing=8, vertical_alignment=ft.CrossAxisAlignment.END, expand=True)]
        if self.split_workspace:
            head.append(ft.IconButton(ft.Icons.CLOSE, icon_color=INK_3, icon_size=18, on_click=self.back,
                                      tooltip="Close details"))
        checked = (item["checked_at"].replace("T", " ").removesuffix("+00:00") + " UTC") if item.get("checked_at") else None
        now = datetime.now(UTC)  # One reading per render; relative times never schedule their own updates.
        status = self.status_mark(item["status"], size=14)
        stuck = unfinished_for(item, now)
        if stuck:
            status = ft.Row([status, self.text(stuck, 12, INK_2, mono=True)], spacing=14, wrap=True, run_spacing=4,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER)
        proof_header = [self.section_title("Proof", self.text(f"Checked {checked}", 11.5, INK_3, mono=True)
                                           if checked else None)]
        recheck = self.recheck_line(item)
        if recheck is not None:
            proof_header.append(recheck)
        recipient = ft.Column([
            self.text("CCTP mint recipient" if downstream else "Fixed recipient", 12, INK_3),
            self.text(item["recipient"], 12.5, INK, mono=True, selectable=True),
            self.text("Arc verifies the CCTP mint. Check downstream actions in the original app." if downstream
                      else "Set by the signed message. The original recipient cannot be changed.", 12, INK_3),
        ], spacing=3)
        sections = [
            ft.Column([ft.Row(head, vertical_alignment=ft.CrossAxisAlignment.START),
                       status,
                       self.text(item.get("explanation") or description, 13, INK_2)], spacing=8),
            ft.Column([*proof_header, self.proof(item, now)], spacing=12),
            recipient,
            self.amounts(item),
            self.next_step(item),
            self.record(item),
        ]
        self.review_host.content = self.transaction_review(self.state.prepared) if self.state.prepared else None
        self.review_host.visible = bool(self.state.prepared)
        controls = []
        for index, section in enumerate(sections):
            if index:
                controls.append(ft.Divider(color=RULE, height=1, thickness=1))
            controls.append(section)
        controls.append(self.review_host)
        return ft.Container(ft.Column(controls, spacing=20, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
                            bgcolor=SURFACE, padding=ft.Padding.only(left=24, right=24, top=18, bottom=28) if not self.narrow
                            else ft.Padding.only(left=16, right=16, top=8, bottom=28))

    def record(self, item):
        managed = bool(item.get("delivery_mode"))
        downstream = item.get("delivery_mode") == "forwarding_with_app_hook"
        facts = [self.fact("Source transaction", item["tx_hash"]),
                 self.fact("Destination caller", "Any caller" if item.get("caller") == ZERO else item.get("caller", "Unknown")),
                 self.fact("Message nonce", item.get("nonce") or "Not available yet")]
        if managed:
            facts.append(self.fact("Delivery", "Circle forwarding with app hook" if downstream else "Circle forwarding",
                                   mono=False))
        if item.get("forward_state"):
            facts.append(self.fact("Circle delivery report", item["forward_state"].title(), mono=False))
        if item.get("discovery_source"):
            discovery = {"stream": "Stream delivery", "example_stream": "Simulated Stream delivery", "rpc": "Source RPC history",
                         "receipt": "Base Sepolia receipt"}[item["discovery_source"]]
            facts.append(self.fact("Discovered via", discovery, mono=False))
        actions = []
        if item.get("checked_at") and not self.state.example:
            actions.append(self.link("Copy evidence", self.copy_evidence, icon=ft.Icons.CONTENT_COPY, flush=True))
            if self.case_link(item):
                actions.append(self.link("Copy case link", self.copy_case_link, icon=ft.Icons.LINK, flush=True))
        controls = [self.section_title("Record"), *facts]
        if actions:
            controls.append(ft.Row(actions, wrap=True, spacing=16))
        return ft.Column(controls, spacing=8)

    async def copy_evidence(self, _=None) -> None:
        item = self.state.selected
        if not item or self.state.example or not item.get("checked_at"):
            return
        fields = [
            "Rebound · Base Sepolia → Arc Testnet",
            f"Checked at {item['checked_at']}",
            f"Status: {state_copy(item['status'])[0]}",
            f"Source transaction: {item['tx_hash']}",
            f"Source block: {item['block_number']} · log {item['log_index']}",
            f"Source receipt: {item['source_url']}",
            f"CCTP mint recipient: {item['recipient']}",
            f"Burn amount: {item['amount']} USDC",
        ]
        if item.get("nonce"):
            fields.extend([f"CCTP nonce: {item['nonce']}", f"Attested fee: {item['fee']} USDC",
                           f"CCTP mint amount: {item['net_amount']} USDC"])
        if item.get("delivery_mode") == "forwarding_with_app_hook":
            fields.append("Check downstream app actions separately from the CCTP mint.")
        if item.get("destination_block") is not None:
            nonce_status = "consumed" if item["nonce_used"] else "unused"
            fields.append(f"Arc nonce {nonce_status} as of block {item['destination_block']}")
        if item.get("destination_url"):
            fields.append(f"Verified Arc receipt: {item['destination_url']}")
        submitted_url = self.submitted_receipt_url(item)
        if submitted_url:
            fields.append(f"Verified submitted Arc receipt: {submitted_url}")
        await self.clipboard.set("\n".join(fields))
        self.page.show_dialog(ft.SnackBar(content=self.text("Transfer evidence copied", color="#FFFFFF")))

    def case_link(self, item: dict) -> str | None:
        page_url = urlsplit(getattr(self.page, "url", "") or "")
        scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(page_url.scheme)
        if scheme is None or not page_url.netloc or item.get("example"):
            return None
        case_path = f"{page_url.path.rstrip('/')}/transfer/{item['tx_hash']}/{item['log_index']}"
        return urlunsplit((scheme, page_url.netloc, case_path, "", ""))

    async def copy_case_link(self, _=None) -> None:
        item = self.state.selected
        link = self.case_link(item) if item else None
        if link:
            await self.clipboard.set(link)
            self.page.show_dialog(ft.SnackBar(content=self.text("Live case link copied", color="#FFFFFF")))

    def search(self, _=None):
        self.state.query = self.search_input.value or ""
        selection_cleared = self.clear_hidden_selection()
        # Keep the search field mounted: replacing its ancestors on each keypress
        # loses Flutter keyboard focus and selection after the first character.
        self.ledger_host.content = self.ledger_content()
        self.ledger_host.update()
        if selection_cleared and self.split_workspace:
            self.inspector_host.content = self.inspector_placeholder()
            self.inspector_host.update()

    def clear_hidden_selection(self):
        selected = self.state.selected
        if selected and not any(item["key"] == selected["key"] for item in self.state.visible_transfers()):
            self.state.selected = None
            self.state.prepared = None
            self.review_error = ""
            self.clear_receipt_editor()
            return True
        return False

    def dialog(self, title, paragraphs, action):
        return ft.AlertDialog(
            title=self.text(title, 18, INK, ft.FontWeight.W_700),
            content=ft.Column([self.text(body, size, color) for body, size, color in paragraphs],
                              spacing=14, tight=True, width=480),
            actions=[self.link(action, lambda _: self.page.pop_dialog())],
            bgcolor=SURFACE, shape=ft.RoundedRectangleBorder(radius=8), scrollable=True)

    def coverage(self, _=None):
        self.page.show_dialog(self.dialog("Supported route", [
            ("Native USDC over CCTP V2, from Base Sepolia to Arc Testnet.", 14, INK),
            (self.state.coverage or "Scan a public address to inspect the exact source block range. The app covers supported CCTP V2 burns; it does not search every bridge or unknown router.", 13, INK_2),
            ("An empty result is not proof that no unfinished transfer exists outside this coverage.", 13, INK_2),
        ], "Close"))

    def next_step(self, item, _description=None):
        ready = item["status"] in {"ready", "caller_required"}
        controls = [self.section_title("Finish the transfer" if ready and not self.state.example else "Actions")]
        if self.state.example:
            controls.append(ft.Container(
                self.text("This example cannot prepare or sign a transaction.", 13, AMBER, ft.FontWeight.W_600),
                bgcolor=AMBER_WASH, border=ft.Border.all(1, AMBER_RULE), border_radius=6,
                padding=ft.Padding.symmetric(horizontal=12, vertical=9)))
        elif ready:
            controls.extend([
                self.text("Review a destination mint using the address that will actually sign it.", 13, INK_2),
                self.caller_input,
                self.button("Checking recovery…" if self.preparing else "Review recovery", self.prepare,
                            primary=True, disabled=self.preparing or self.refreshing),
                self.text("No signing happens in this page. Keep private keys out of your browser.", 12, INK_3),
            ])
            if self.review_error:
                controls.append(self.text(self.review_error, 13, RED, ft.FontWeight.W_600))
        elif item["status"] == "expired":
            controls.append(self.link("Circle re-attestation guide", icon=ft.Icons.OPEN_IN_NEW,
                                      url="https://developers.circle.com/cctp/references/technical-guide#message-body"))
        if self.receipt_editor_available(item):
            verifying = self._receipt_request is not None
            self.receipt_hash_input.disabled = verifying
            controls.extend([
                self.text("Arc already used this nonce. Paste the Arc transaction that finished it to check "
                          "that receipt against the original message.", 13, INK_2),
                self.receipt_hash_input,
                self.button("Verifying receipt…" if verifying else "Verify Arc receipt", self.verify_arc_receipt,
                            primary=True, disabled=verifying or self.refreshing or self.state.busy),
            ])
            if self.receipt_error:
                controls.append(self.text(self.receipt_error, 13, RED, ft.FontWeight.W_600))
        buttons = []
        if not self.state.example:
            buttons.append(self.button("Checking transfer…" if self.refreshing else "Refresh this transfer",
                                       self.refresh_transfer, disabled=self.refreshing or self.preparing or self.state.busy
                                       or self._receipt_request is not None))
        replayable = (not self.state.example and item["status"] == "completed"
                      and item.get("destination_url") and not item.get("delivery_mode"))
        if replayable:
            buttons.append(self.button("Replaying mint…" if self.replaying else "Replay historical mint",
                                       self.replay_mint, disabled=self.replaying))
        if buttons:
            controls.append(ft.Row(buttons, spacing=8, wrap=True, run_spacing=8))
        if self.refresh_error and not self.state.example:
            controls.append(self.text(self.refresh_error, 13, RED, ft.FontWeight.W_600))
        if replayable and self.replay_item is item and self.replay_result:
            proof = self.replay_result
            controls.extend([
                self.fact(f"Arc block {proof['previous_block']:,}", "Nonce unused, mint simulation passed", mono=False),
                self.fact(f"Arc block {proof['mint_block']:,}", "Nonce consumed, receipt verified", mono=False),
                self.text("Replay used historical chain reads and simulation.", 12, INK_3),
            ])
        if replayable and self.replay_item is item and self.replay_error:
            controls.append(self.text(self.replay_error, 13, RED, ft.FontWeight.W_600))
        return ft.Column(controls, spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def value_line(self, label, value, selectable=False):
        return ft.Column([self.text(label, 12, INK_3), self.text(value, 13, INK, ft.FontWeight.W_500,
                           selectable=selectable)], spacing=2)

    def transaction_review(self, prepared):
        item = self.state.selected
        transaction = json.dumps(prepared["transaction"], indent=2)
        command = f"uv run python -m rebound.cli recover {item['tx_hash']} {item['log_index']} --caller {prepared['transaction']['from']}"
        gas = prepared.get("gas_quote")
        gas_controls = []
        if gas:
            gas_controls = [
                self.fact("Arc gas budget", display_amount(gas["max_cost_usdc"]) + " testnet USDC"),
                self.fact("Caller’s Arc balance", display_amount(gas["caller_balance_usdc"]) + " testnet USDC"),
            ]
            if gas["shortfall_usdc"] != "0":
                gas_controls.extend([
                    self.text(f"Add {display_amount(gas['shortfall_usdc'])} testnet USDC to cover this gas budget.",
                              13, AMBER, ft.FontWeight.W_600),
                    self.link("Open Circle testnet faucet", url="https://faucet.circle.com/", icon=ft.Icons.OPEN_IN_NEW),
                ])

        async def copy(_):
            await self.clipboard.set(transaction)
        return ft.Container(ft.Column([
            self.text("Unsigned recovery transaction", 15, INK, ft.FontWeight.W_700),
            self.text("The source transfer, destination nonce and caller were rechecked, and the mint was simulated. This transaction has not been signed or sent.", 13, INK_2),
            self.fact("Recipient", prepared.get("recipient", item["recipient"])),
            self.fact("Destination amount", display_amount(prepared.get("net_amount")) + " USDC"),
            *gas_controls,
            ft.TextField(value=transaction, read_only=True, multiline=True, min_lines=6, max_lines=10,
                         **field_style(fill_color=PAPER, border_color=RULE,
                                       text_style=ft.TextStyle(size=12, font_family=MONO, color=INK))),
            ft.Row([self.button("Copy transaction JSON", copy, icon=ft.Icons.CONTENT_COPY)], wrap=True),
            self.text("To sign locally", 13, INK, ft.FontWeight.W_700),
            ft.Container(self.text(command, 12, INK, mono=True, selectable=True), bgcolor=PAPER,
                         border=ft.Border.all(1, RULE), border_radius=4, padding=10),
            self.text("The local command revalidates the transfer again and asks for confirmation before sending on Arc Testnet. Never paste a private key into this app.", 13, INK_2),
            self.text(prepared.get("warning") or "A successful simulation does not guarantee later execution. Competing attempts can still consume gas.", 12, AMBER, ft.FontWeight.W_600),
        ], spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), border=ft.Border.all(1, RULE_STRONG),
            border_radius=8, padding=16)

    async def scan(self, _=None, *, mode: str = "wallet", log_index: int | None = None) -> None:
        if self.state.busy:
            return
        self.clear_receipt_editor()
        self.state.busy = True
        self.render()
        search = self.tx_input.value if mode == "transaction" else self.wallet_input.value or self.state.wallet
        await self.state.scan(self.service, search or "", int(self.history.value or "2000"),
                              self.end_block.value or "", mode=mode, log_index=log_index)
        self.render()

    async def scan_index(self, _=None) -> None:
        await self.scan(mode="stream")

    async def live_sample(self, _=None) -> None:
        if self.state.busy:
            return
        self.show_transaction = False
        self.show_history = True
        self.wallet_input.value = LIVE_SAMPLE_WALLET
        self.end_block.value = str(LIVE_SAMPLE_END_BLOCK)
        self.history.value = str(LIVE_SAMPLE_BLOCKS)
        await self.scan()

    async def inspect_transaction(self, _=None) -> None:
        await self.scan(mode="transaction")

    async def route_changed(self, event) -> None:
        await self.navigate(event.route)

    async def navigate(self, route: str) -> None:
        if route == self._active_route:
            return
        initial_mount = self._active_route is None
        self._active_route = route
        # Flet can reconnect an existing page session after browser navigation.
        # Drop the previous page's financial state before processing its new URL.
        # In-flight reads keep their old InboxState and cannot replace this route.
        self.state = InboxState()
        self.drop_recheck()
        self.clear_receipt_editor()
        self.review_error = ""
        self.refresh_error = ""
        self.show_transaction = False
        self.render()
        await self.open_case(route, reset_scroll=not initial_mount)

    async def open_case(self, route: str, *, reset_scroll: bool = True) -> None:
        if route.partition("?")[0].rstrip("/") == "/demo":
            self.examples()
            return
        if not route.startswith("/transfer/"):
            return
        path = urlsplit(route).path
        self.show_transaction = True
        match = CASE_PATH.fullmatch(path)
        if match is None:
            self.state.error = "This case link is incomplete. Search using the original Base Sepolia transaction hash."
            self.render()
            return
        tx_hash, index = match.groups()
        self.tx_input.value = tx_hash.lower()
        state = self.state
        await self.scan(mode="transaction", log_index=int(index))
        if self.state is state and len(state.transfers) == 1:
            item = state.transfers[0]
            if (item["tx_hash"], item["log_index"]) == (tx_hash.lower(), int(index)):
                self.inspect(item, reset_scroll=reset_scroll)

    def submitted_receipt_url(self, item: dict) -> str | None:
        if self.state.example or item.get("example") or item.get("submitted_receipt_verified") is not True:
            return None
        return item.get("submitted_receipt_url") or None

    def receipt_editor_available(self, item: dict) -> bool:
        return (not self.state.example and not item.get("example")
                and not str(item.get("key", "")).startswith("example-")
                and item.get("status") == "completed" and not item.get("destination_url")
                and not self.submitted_receipt_url(item))

    def clear_receipt_editor(self) -> None:
        self.receipt_hash_input.value = ""
        self.receipt_error = ""
        self._receipt_request = None

    async def verify_arc_receipt(self, _=None) -> None:
        state, item = self.state, self.state.selected
        if (item is None or not self.receipt_editor_available(item) or self._receipt_request is not None
                or state.busy or self.refreshing or self.preparing):
            return
        try:
            destination_hash = hash32((self.receipt_hash_input.value or "").strip())
        except ValueError as exc:
            self.receipt_error = str(exc)
            self.render()
            return
        request = object()
        self._receipt_request = request
        self.receipt_error = ""
        self.render()

        def still_selected():
            return (self._receipt_request is request and self.state is state and state.selected is item
                    and not state.example and not item.get("example")
                    and (self.receipt_hash_input.value or "").strip().lower() == destination_hash)

        try:
            result = await self.service.inspect_transaction(item["tx_hash"], log_index=item["log_index"],
                                                             destination_hash=destination_hash)
            if not still_selected():
                return
            transfers = result["transfers"]
            if (len(transfers) != 1 or transfers[0]["key"] != item["key"]
                    or (transfers[0]["tx_hash"], transfers[0]["log_index"]) != (item["tx_hash"], item["log_index"])
                    or transfers[0].get("submitted_destination_hash") != destination_hash):
                raise ValueError("The receipt check did not return this original transfer and submitted hash.")
            fresh = transfers[0] | {"discovery_source": item.get("discovery_source", "receipt")}
            if fresh["status"] != "completed":
                raise ValueError("Receipt verification is unavailable. Showing the previous transfer result; refresh and try again.")
            state.transfers = [fresh if row["key"] == item["key"] else row for row in state.transfers]
            state.selected = fresh
            if not self.submitted_receipt_url(fresh):
                self.receipt_error = "Arc receipt not verified. Check the transaction hash and try again after it confirms."
        except Exception as exc:
            if still_selected():
                self.receipt_error = safe_error(exc)
        finally:
            if self._receipt_request is request:
                self._receipt_request = None
                self.render()

    def recheck_eligible(self, item: dict | None) -> bool:
        return (item is not None and not self.state.example and not item.get("example")
                and not str(item.get("key", "")).startswith("example-")
                and item.get("status") in RECHECK_STATUSES)

    def rechecking(self, run: Recheck | None) -> bool:
        """True while this run still belongs to the selected, waiting, error-free transfer."""
        item = self.state.selected
        return (run is not None and run is self._recheck and run.active and run.state is self.state
                and item is not None and item.get("key") == run.key and self.recheck_eligible(item)
                and not self.refresh_error)

    def recheck_label(self, run: Recheck) -> str:
        return "Checking now" if self.refreshing else f"Checking again in {run.remaining} s"

    def recheck_line(self, item: dict):
        self._recheck_text = None
        run = self._recheck
        if run is None or run.state is not self.state or item.get("key") != run.key:
            return None
        if run.note:
            return self.text(run.note, 12, INK_3) if self.recheck_eligible(item) else None
        if not self.rechecking(run):
            return None
        self._recheck_text = self.text(self.recheck_label(run), 12, INK_2, mono=True)
        stop = self.link("Stop", self.stop_recheck, color=INK_2, flush=True)
        stop.style.alignment = ft.Alignment.CENTER_RIGHT  # Keep the short label on the proof's right edge.
        return ft.Row([self._recheck_text, ft.Container(expand=True), stop], spacing=12)

    def start_recheck(self) -> None:
        item = self.state.selected
        if not self._installed or not self.recheck_eligible(item):
            return
        self._recheck = Recheck(self.state, item["key"], self.recheck_clock())
        self.page.run_task(self.recheck_loop, self._recheck)

    def drop_recheck(self, _=None) -> None:
        self._recheck = None

    def stop_recheck(self, _=None) -> None:
        if self._recheck is not None:
            self.drop_recheck()
            self.render()

    def show_recheck_countdown(self, run: Recheck) -> None:
        line = self._recheck_text
        if line is None or not self.rechecking(run):
            return
        line.value = self.recheck_label(run)
        try:
            line.update()
        except RuntimeError as exc:
            # The countdown is cosmetic. Before the line reaches the client, the next render shows it.
            if "must be added to the page" not in str(exc):
                raise

    async def recheck_loop(self, run: Recheck) -> None:
        """Recheck the selected waiting transfer every RECHECK_SECONDS, one request at a time."""
        try:
            while self.rechecking(run):
                for remaining in range(RECHECK_SECONDS, 0, -1):
                    run.remaining = remaining
                    self.show_recheck_countdown(run)
                    await self.recheck_sleep(1)
                    if not self.rechecking(run):
                        return
                if self.recheck_clock() - run.started >= RECHECK_LIMIT_SECONDS:
                    run.active = False
                    run.note = "Stopped checking after 10 min. Refresh this transfer to check again."
                    self.render()
                    return
                if (not self.client_connected or self.state.busy or self.refreshing or self.preparing
                        or self._receipt_request is not None):
                    continue
                run.remaining = RECHECK_SECONDS
                await self.refresh_transfer()
        finally:
            run.active = False

    async def refresh_transfer(self, _=None) -> None:
        state, item = self.state, self.state.selected
        if state.busy or state.example or item is None or self.refreshing or self.preparing or self._receipt_request is not None:
            return
        self.refreshing = True
        self.refresh_error = ""
        state.prepared = None
        self.render()
        try:
            options = {"destination_hash": item["submitted_destination_hash"]} if self.submitted_receipt_url(item) else {}
            result = await self.service.inspect_transaction(item["tx_hash"], log_index=item["log_index"], **options)
            transfers = result["transfers"]
            if len(transfers) != 1 or transfers[0]["key"] != item["key"]:
                raise ValueError("The original supported burn was not found in its current source receipt.")
            if self.state is state and state.selected is item:
                fresh = transfers[0] | {"discovery_source": item.get("discovery_source", "receipt")}
                state.transfers = [fresh if row["key"] == item["key"] else row for row in state.transfers]
                state.selected = fresh
                # Keep the updated transfer visible after a state change.
                if fresh not in state.visible_transfers():
                    state.filter = "all"
                    state.query = ""
        except Exception as exc:
            if self.state is state and state.selected is item:
                self.refresh_error = f"Refresh failed. Showing the previous result. {safe_error(exc)}"
        finally:
            self.refreshing = False
            self.render()

    async def replay_mint(self, _=None) -> None:
        item = self.state.selected
        if self.replaying or item is None or self.state.example or item.get("status") != "completed":
            return
        self.replaying = True
        self.replay_item = item
        self.replay_result = None
        self.replay_error = ""
        self.render()
        try:
            self.replay_result = await self.service.replay_mint(item["tx_hash"], item["log_index"])
        except Exception as exc:
            self.replay_error = safe_error(exc)
        finally:
            self.replaying = False
            self.render()

    def toggle_transaction(self, _=None) -> None:
        self.show_transaction = not self.show_transaction
        self.render()

    def toggle_history(self, _=None):
        self.show_history = not self.show_history
        self.render()

    def examples(self, _=None) -> None:
        self.clear_receipt_editor()
        self.show_transaction = False
        self.wallet_input.value = EXAMPLE_WALLET
        self.state.load_examples()
        self.render()

    def example_stream(self, _=None) -> None:
        if not self.state.example or self.state.busy:
            return
        self.wallet_input.value = EXAMPLE_WALLET
        self.state.load_examples(stream=True)
        self.inspect(self.state.transfers[0])

    async def copy_demo_wallet(self, _=None) -> None:
        if self.state.example:
            await self.clipboard.set(EXAMPLE_WALLET)
            self.page.show_dialog(ft.SnackBar(content=self.text("Demo wallet copied", color="#FFFFFF")))

    def return_live(self, _=None) -> None:
        self.clear_receipt_editor()
        if (self.wallet_input.value or "").strip().lower() == EXAMPLE_WALLET:
            self.wallet_input.value = self.state.wallet
        self.state = InboxState(wallet=self.state.wallet)
        self.render()

    def set_filter(self, key) -> None:
        self.state.filter = key
        self.clear_hidden_selection()
        self.render()

    def inspect(self, item, *, reset_scroll: bool = True) -> None:
        changed = item is not self.state.selected
        if changed:
            self.clear_receipt_editor()
            self.drop_recheck()
        self.state.selected = item
        self.state.prepared = None
        self.caller_input.value = ""
        self.review_error = ""
        self.refresh_error = ""
        if changed:
            self.start_recheck()  # Before render, so the first frame already shows the countdown.
        self._revealing_item = item if changed and self.motion_enabled else None
        self.render()
        if self._installed and changed and (reset_scroll or self.motion_enabled):
            self.page.run_task(self.finish_inspector_reveal, item, reset_scroll)

    async def finish_inspector_reveal(self, item: dict, reset_scroll: bool = True) -> None:
        if self._revealing_item is item:
            await asyncio.sleep(0.035)
            if self.state.selected is not item or self._revealing_item is not item:
                return
            self._revealing_item = None
            self.inspector_host.animate_opacity = ft.Animation(180, ft.AnimationCurve.EASE_OUT_CUBIC)
            self.inspector_host.opacity = 1
            self.inspector_host.update()
        if reset_scroll and self.state.selected is item:
            detail_scroll = self.inspector_scroll if self.split_workspace else self.workspace_view
            try:
                await detail_scroll.scroll_to(offset=0, duration=0)
            except TimeoutError:
                return  # The visual reset may outlive the client connection.
            except RuntimeError as exc:
                # A cosmetic reset can race a browser disconnect or the Flutter
                # listener mounting. Do not mask unrelated UI/programming errors.
                if str(exc) != "Session closed" and not re.fullmatch(
                    r"(?:TimeoutException after [0-9:.]+: )?"
                    r"Timeout waiting for invoke method listener for Column\([0-9]+\)\.scroll_to",
                    str(exc),
                ):
                    raise

    def back(self, _=None) -> None:
        self.clear_receipt_editor()
        self.drop_recheck()
        self.state.selected = None
        self.state.prepared = None
        self.render()

    def caller_changed(self, _=None) -> None:
        if self.state.prepared is not None:
            self.state.prepared = None
            self.review_host.content = None
            self.review_host.visible = False
            self.review_host.update()

    async def prepare(self, _=None) -> None:
        if self.preparing or self.refreshing or self.state.selected is None:
            return
        state, transfer = self.state, self.state.selected
        caller = self.caller_input.value or ""

        def still_selected() -> bool:
            return (self.state is state and state.selected is transfer and not state.example
                    and (self.caller_input.value or "") == caller)

        self.preparing = True
        self.review_error = ""
        state.prepared = None
        self.render()
        try:
            prepared = await state.prepare(self.service, transfer, caller)
            if still_selected():
                state.prepared = prepared
        except Exception as exc:
            if still_selected():
                self.review_error = safe_error(exc)
        finally:
            self.preparing = False
            self.render()

    def about(self, _=None) -> None:
        self.page.show_dialog(self.dialog("How Rebound works", [
            ("Rebound discovers supported source burns, asks Circle for their signed messages, and checks whether Arc has already consumed each nonce.", 14, INK_2),
            ("An eligible recovery completes that same transfer to the recipient encoded in its message. It does not send USDC a second time or change the recipient.", 14, INK_2),
            ("Coverage is limited to the displayed route and history range. Unknown router contracts, unsupported messages and restricted callers can need a different recovery path.", 13, INK_2),
        ], "Close"))


def build_main(service):
    """Return the async page entrypoint expected by flet.fastapi.app()."""
    async def main(page: ft.Page):
        ui = InboxUI(page, service)
        ui.install()
        await ui.load_motion_preference()
        await ui.navigate(page.route)
    return main
