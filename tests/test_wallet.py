import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from starlette.routing import Mount

from rebound import protocol as p
from rebound import ui
from rebound.app import create_app

TX = "0x" + "cd" * 32
PAGE = f"/finish/{TX}/4"


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def wallet_app():
    # The page itself never calls the service; a bare stand-in proves that.
    return create_app(SimpleNamespace(settings=SimpleNamespace(stream_enabled=False)), include_ui=False)


def page_parts(response):
    policy = response.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", policy).group(1)
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", response.text, re.S)
    return policy, nonce, scripts


async def test_finish_page_is_served_with_strict_security_headers():
    async with client(wallet_app()) as http:
        response = await http.get(PAGE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    policy, nonce, _ = page_parts(response)
    directives = dict(item.strip().split(" ", 1) for item in policy.split(";"))
    assert directives == {
        "default-src": "'none'", "script-src": f"'nonce-{nonce}'", "style-src": f"'nonce-{nonce}'",
        "connect-src": "'self' https://rpc.testnet.arc.network", "img-src": "'self' data:",
        "font-src": "'self'", "frame-ancestors": "'none'", "base-uri": "'none'", "form-action": "'none'",
    }
    assert "unsafe" not in policy


async def test_csp_nonce_matches_every_inline_block_and_changes_per_response():
    async with client(wallet_app()) as http:
        first, second = await http.get(PAGE), await http.get(PAGE)
    _, nonce, scripts = page_parts(first)
    assert nonce != page_parts(second)[1]
    assert [attributes for attributes, _ in scripts] == [f' nonce="{nonce}"']
    styles = re.findall(r"<style([^>]*)>", first.text)
    assert styles == [f' nonce="{nonce}"']
    assert " style=" not in first.text  # Inline style attributes would be blocked by the policy.


async def test_page_loads_nothing_from_other_origins_and_never_handles_keys():
    async with client(wallet_app()) as http:
        body = (await http.get(PAGE)).text
    assert not re.search(r"<script[^>]*\bsrc=", body)
    assert not re.search(r"<link[^>]*rel=\"stylesheet\"", body)
    assert not re.search(r"""url\(["']?(https?:)?//""", body)
    assert "<form" not in body and "<input" not in body and "<textarea" not in body
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in body
    assert "eth_sign\"" not in body and "personal_sign" not in body and "eth_signTypedData" not in body


async def test_page_injects_the_fixed_contract_chain_and_normalized_transfer():
    async with client(wallet_app()) as http:
        response = await http.get(f"/finish/{TX.upper().replace('0X', '0x')}/4")
    _, _, scripts = page_parts(response)
    config = json.loads(re.search(r"const CONFIG = (\{.*?\});", scripts[0][1]).group(1))
    assert config == {
        "txHash": TX, "logIndex": 4, "transmitter": p.TRANSMITTER, "chainId": 5042002,
        "chainHex": "0x4cef52", "selector": "0x57ecfd28", "rpc": "https://rpc.testnet.arc.network",
        "explorer": p.ARC_EXPLORER, "baseExplorer": "https://sepolia.basescan.org",
    }
    assert config["selector"] == "0x" + p.RECEIVE_SELECTOR.hex()


@pytest.mark.parametrize("path", [
    "/finish/0x1234/4", f"/finish/{TX[:-1]}g/4", f"/finish/{TX}00/4", f"/finish/{TX}/-1",
    f"/finish/{TX}/abc", f"/finish/{TX}/4.0", f"/finish/{TX}/100001", f"/finish/{TX}/0x4",
])
async def test_malformed_transfer_identity_is_rejected(path):
    async with client(wallet_app()) as http:
        response = await http.get(path)
    assert response.status_code == 400
    assert "content-security-policy" not in response.headers


async def test_unmatched_finish_paths_do_not_render_the_page():
    async with client(wallet_app()) as http:
        assert (await http.get(f"/finish/{TX}")).status_code == 404
        assert (await http.get(f"/finish/{TX}/4/extra")).status_code == 404


async def test_bundled_font_is_served_from_the_same_origin():
    async with client(wallet_app()) as http:
        response = await http.get("/finish/manrope.ttf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/ttf"
    assert response.content[:4] == b"\x00\x01\x00\x00"


async def test_wallet_routes_precede_the_flet_catch_all_mount():
    app = create_app(SimpleNamespace(settings=SimpleNamespace(stream_enabled=False)))
    assert isinstance(app.router.routes[-1], Mount)
    async with client(app) as http:
        page, font = await http.get(PAGE), await http.get("/finish/manrope.ttf")
    assert page.status_code == 200 and "content-security-policy" in page.headers
    assert "<title>Rebound · Finish transfer</title>" in page.text
    assert font.headers["content-type"] == "font/ttf"


async def test_inline_script_is_valid_javascript(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    async with client(wallet_app()) as http:
        _, _, scripts = page_parts(await http.get(PAGE))
    source = tmp_path / "finish.js"
    source.write_text(scripts[0][1], encoding="utf-8")
    result = subprocess.run([node, "--check", str(source)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_inbox_offers_the_wallet_page_only_for_live_ready_transfers():
    class Page:
        url = "https://rebound.example/"

    app = ui.InboxUI(Page(), object())
    ready = {"key": "live", "tx_hash": TX, "log_index": 4, "status": "ready", "example": False}
    button = app.finish_button(ready)
    assert button.url == f"https://rebound.example/finish/{TX}/4"
    assert button.content == "Finish in browser wallet"
    assert app.finish_button(ready | {"example": True}) is None
    for status in ("completed", "caller_required", "awaiting_attestation", "expired"):
        assert app.finish_button(ready | {"status": status}) is None
    app.state.example = True
    assert app.finish_button(ready) is None
