"""One-time code transport fixtures; all provider calls are synthetic."""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from rebound.app import create_app
from rebound.wallet_session import sessions_for
from tests.test_protocol import WALLET

pytest_plugins = ["tests.test_service"]
NODE = shutil.which("node")
TOKEN, CODE = "t" * 22, "c" * 22


@pytest.fixture
def enabled_service(setup_service):
    service, _ = setup_service
    service.settings = replace(service.settings, browser_wallet=True)
    return service


async def test_exchange_is_single_use_and_header_auth_never_uses_logged_paths(enabled_service):
    service = enabled_service
    session = sessions_for(service).create("0x" + "ab" * 32, 3)
    code = session.code
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, include_ui=False)),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/wallet-session/exchange", json={"code": code})
        assert response.status_code == 200 and response.json()["token"] == session.id
        again = await client.post("/api/wallet-session/exchange", json={"code": code})
        assert again.status_code == 404 and again.headers["cache-control"] == "no-store"
        state = await client.get(
            "/api/wallet-session", headers={"Authorization": "Bearer " + session.id}
        )
        assert state.status_code == 200 and state.json()["state"] == "created"
        assert session.id not in str(state.request.url) and session.id not in state.text
        assert (
            await client.get(
                "/api/wallet-session", headers={"X-Rebound-Wallet-Session": session.id}
            )
        ).status_code == 401
        duplicate = await client.get(
            "/api/wallet-session",
            headers=[
                ("Authorization", "Bearer " + session.id),
                ("Authorization", "Bearer " + session.id),
            ],
        )
        assert duplicate.status_code == 401


@pytest.mark.parametrize(
    "authorization", [None, "Basic abc", "Bearer short", "Bearer " + TOKEN + " trailing"]
)
async def test_all_state_endpoints_require_wellformed_bearer_and_errors_are_uncacheable(
    enabled_service, authorization
):
    routes = [
        ("GET", "", None),
        ("POST", "/connected", {"account": WALLET, "chainId": "0x4cef52"}),
        ("POST", "/claim", {}),
        ("POST", "/result", {"outcome": "unknown"}),
    ]
    headers = {} if authorization is None else {"Authorization": authorization}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(enabled_service, include_ui=False)),
        base_url="http://test",
    ) as client:
        for method, suffix, body in routes:
            response = await client.request(
                method, "/api/wallet-session" + suffix, headers=headers, json=body
            )
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["www-authenticate"] == "Bearer"
            assert authorization is None or authorization not in response.text
        unknown = await client.get(
            "/api/wallet-session", headers={"Authorization": "Bearer " + TOKEN}
        )
        assert unknown.status_code == 404 and unknown.headers["cache-control"] == "no-store"


HARNESS = r"""
const vm=require('vm'),fs=require('fs');
const script=fs.readFileSync(process.argv[2],'utf8'),s=JSON.parse(process.argv[3]);
const calls=[],requests=[],events=[],listeners={},status={textContent:''};
const location={origin:'https://rebound.example',pathname:s.path||'/wallet/adapter',hash:s.hash,search:''};
location.href=location.origin+location.pathname+location.hash;
let account=s.account,chain=s.chain||'0x4cef52',switches=0,connecting=0,maxConnecting=0,connectCount=0;
const provider={async request(request){
 calls.push(request);events.push(request.method);
 if(request.method==='eth_requestAccounts'||request.method==='eth_accounts')return [account];
 if(request.method==='eth_chainId')return chain;
 if(request.method==='wallet_switchEthereumChain'){
   if(++switches===1){const e=Error('unknown');e.code=4902;throw e;}
   if(!s.refuseSwitch)chain='0x4cef52';return null;
 }
 if(request.method==='wallet_addEthereumChain'){if(s.addSelects)chain='0x4cef52';return null;}
 throw Error('Unexpected provider method: '+request.method);
},on(event,callback){listeners[event]=callback;}};
const history={replaceState(_s,_t,path){events.push('clear');if(s.historyFails)throw Error('blocked');location.hash='';location.href=location.origin+path;}};
async function fetch(url,options){
 requests.push({url:String(url),options});events.push('fetch');
 const path=new URL(url).pathname;
 if(path.endsWith('/exchange')){
   if(s.unknown)return {ok:false,status:404,json:async()=>({detail:'gone'})};
   return {ok:true,status:200,json:async()=>({token:s.badToken?'short':s.token,state:'created'})};
 }
 if(path.endsWith('/connected')){
   ++connectCount;++connecting;maxConnecting=Math.max(maxConnecting,connecting);
   if(s.changeDuringConnect&&connectCount===1){
     setTimeout(()=>{account='0x'+'22'.repeat(20);if(listeners.accountsChanged)listeners.accountsChanged([account]);if(listeners.chainChanged)listeners.chainChanged(chain);},5);
     await new Promise(resolve=>setTimeout(resolve,30));
   }
   --connecting;
   if(s.reconnectFails && connectCount>1)return {ok:false,status:409,json:async()=>({detail:'review changed'})};
 }
 return {ok:true,status:200,json:async()=>({state:s.approved?'approved':s.changeDuringConnect?'reviewed':'confirmed'})};
}
const window={location,history,ethereum:provider,addEventListener(){}};
vm.runInNewContext(script,{window,document:{getElementById:()=>status},fetch,URL,setTimeout,setInterval,clearInterval,BigInt,Promise,JSON,Number,String,Array,Object,Error});
setTimeout(()=>{console.log(JSON.stringify({calls,requests,events,location,status:status.textContent,maxConnecting}));process.exit(0)},160);
"""


def run_js(tmp_path, **changes):
    config = {
        "header": "Authorization",
        "transmitter": "0xe737e5cebeeba77efe34d4aa090756590b1ce275",
        "chainHex": "0x4cef52",
        "selector": "0x57ecfd28",
        "rpc": "https://rpc.testnet.arc.network",
        "explorer": "https://explorer.testnet.arc.io",
        "maxGas": "2400000",
        "maxNetworkFee": str(10**17),
    }
    source = Path(__file__).resolve().parents[1] / "rebound/wallet_adapter.js"
    script = tmp_path / "adapter.js"
    script.write_text("const CONFIG=" + json.dumps(config) + ";\n" + source.read_text())
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS)
    scenario = {"hash": "#" + CODE, "code": CODE, "token": TOKEN, "account": WALLET} | changes
    result = subprocess.run(
        [NODE, str(harness), str(script), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node unavailable")
@pytest.mark.parametrize(
    "change",
    [
        {"hash": ""},
        {"hash": "#short"},
        {"unknown": True},
        {"badToken": True},
        {"historyFails": True},
    ],
)
def test_unusable_code_or_exchange_never_prompts_and_instructs_reopen(tmp_path, change):
    result = run_js(tmp_path, **change)
    assert not result["calls"]
    assert "Reopen" in result["status"]


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_one_time_fragment_exchange_and_bearer_stay_out_of_urls_and_referrers(tmp_path):
    result = run_js(tmp_path, path="/prefix/wallet/adapter")
    assert result["events"][0] == "clear"
    assert result["location"]["hash"] == ""
    assert CODE not in result["location"]["href"] and TOKEN not in result["location"]["href"]
    for index, request in enumerate(result["requests"]):
        assert request["url"].startswith("https://rebound.example/prefix/api/wallet-session")
        assert TOKEN not in request["url"] and CODE not in request["url"]
        options = request["options"]
        assert options["referrerPolicy"] == "no-referrer" and options["redirect"] == "error"
        assert options["credentials"] == "omit"
        if index == 0:
            assert "Authorization" not in options["headers"]
            assert json.loads(options["body"]) == {"code": CODE}
        else:
            assert options["headers"]["Authorization"] == "Bearer " + TOKEN
            assert CODE not in options.get("body", "") and TOKEN not in options.get("body", "")


@pytest.mark.skipif(NODE is None, reason="node unavailable")
@pytest.mark.parametrize("selects,switches", [(False, 2), (True, 1)])
def test_arc_add_requests_switch_only_if_chain_remains_unselected(tmp_path, selects, switches):
    result = run_js(tmp_path, chain="0x1", addSelects=selects)
    assert (
        len([call for call in result["calls"] if call["method"] == "wallet_switchEthereumChain"])
        == switches
    )
    assert any(request["url"].endswith("/connected") for request in result["requests"])


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_overlapping_events_are_serialized_and_do_not_request_new_permissions(tmp_path):
    result = run_js(tmp_path, changeDuringConnect=True)
    assert len([call for call in result["calls"] if call["method"] == "eth_requestAccounts"]) == 1
    connected = [
        json.loads(row["options"]["body"])
        for row in result["requests"]
        if row["url"].endswith("/connected")
    ]
    assert connected[-1]["account"] == "0x" + "22" * 20
    assert result["maxConnecting"] == 1
    assert not any(call["method"] == "eth_sendTransaction" for call in result["calls"])


@pytest.mark.skipif(NODE is None, reason="node unavailable")
def test_stale_approval_is_not_claimed_after_a_failed_account_reconnect(tmp_path):
    result = run_js(tmp_path, changeDuringConnect=True, reconnectFails=True, approved=True)
    assert not any(request["url"].endswith("/claim") for request in result["requests"])
    assert not any(call["method"] == "eth_sendTransaction" for call in result["calls"])
    assert "Reopen" in result["status"]
