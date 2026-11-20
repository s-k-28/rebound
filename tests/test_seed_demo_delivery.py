"""Local-only script boundary checks; every HTTP request is mocked."""
import json

import httpx
import pytest

from scripts import seed_demo_delivery as seed
from tests.test_protocol import burn_log


@pytest.fixture
def delivery(monkeypatch):
    monkeypatch.setenv("REBOUND_STREAM_SECRET", "unit-test-only-secret")
    monkeypatch.setenv("BASE_SEPOLIA_RPC", "https://base.test/rpc")
    log = burn_log() | {"transactionHash": seed.SOURCE_TX, "logIndex": hex(seed.SOURCE_LOG)}
    receipt = {"status": "0x1", "transactionHash": seed.SOURCE_TX, "blockHash": log["blockHash"],
               "blockNumber": log["blockNumber"], "logs": [log]}
    state = {"rpc": {"result": receipt}, "rpc_status": 200, "ack": {"accepted": 1}, "endpoint_status": 200}
    calls = []

    def post(url, **options):
        calls.append((url, options))
        is_rpc = "json" in options
        return httpx.Response(state["rpc_status"] if is_rpc else state["endpoint_status"],
                              json=state["rpc"] if is_rpc else state["ack"],
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(seed.httpx, "post", post)
    return state, calls, receipt


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765",
                                 "https://demo.example/base/"])
def test_safe_destination_sends_one_validated_log_without_redirects_or_proxy_env(delivery, url):
    _, calls, _ = delivery
    assert seed.main(["seed", url]) == 0
    assert len(calls) == 2
    for _, options in calls:
        assert options["follow_redirects"] is False and options["trust_env"] is False
    target, options = calls[-1]
    assert target == url.rstrip("/") + "/api/streams"
    assert "x-rebound-secret" not in calls[0][1].get("headers", {})
    assert options["headers"] == {"content-type": "application/json", "x-rebound-secret": "unit-test-only-secret"}
    assert len(json.loads(options["content"])["logs"]) == 1


@pytest.mark.parametrize("url", ["http://remote.example", "https://name:password@demo.example", "https://demo.example?x=1",
                                 "https://demo.example/#fragment", "https://demo.example/?", "file:///tmp/test",
                                 "https://demo.example:99999", " https://demo.example", "https://demo.example\n"])
def test_unsafe_destination_is_rejected_before_any_request(delivery, url):
    _, calls, _ = delivery
    assert seed.main(["seed", url]) == 2
    assert calls == []


@pytest.mark.parametrize("change", ["failed", "wrong_receipt_hash", "duplicate", "wrong_log_hash", "wrong_block", "wrong_height",
                                   "wrong_contract", "missing", "malformed", "rpc_error", "http_error"])
def test_bad_receipt_never_sends_the_shared_secret(delivery, change):
    state, calls, receipt = delivery
    if change == "failed":
        receipt["status"] = "0x0"
    elif change == "wrong_receipt_hash":
        receipt["transactionHash"] = "0x" + "12" * 32
    elif change == "duplicate":
        receipt["logs"] *= 2
    elif change == "wrong_log_hash":
        receipt["logs"][0]["transactionHash"] = "0x" + "12" * 32
    elif change == "wrong_block":
        receipt["logs"][0]["blockHash"] = "0x" + "12" * 32
    elif change == "wrong_height":
        receipt["logs"][0]["blockNumber"] = "0x65"
    elif change == "wrong_contract":
        receipt["logs"][0]["address"] = "0x" + "12" * 20
    elif change == "missing":
        receipt["logs"] = []
    elif change == "malformed":
        state["rpc"] = []
    elif change == "rpc_error":
        state["rpc"]["error"] = {"message": "private-provider-body"}
    else:
        state["rpc_status"] = 503
    assert seed.main(["seed"]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("ack", [{"accepted": 0}, {"accepted": 2}, {"accepted": True}, {}, [], None])
def test_only_one_accepted_event_counts_as_success(delivery, ack, capsys):
    state, _, _ = delivery
    state["ack"] = ack
    assert seed.main(["seed"]) == 1
    output = capsys.readouterr().out
    assert "Delivered " not in output and "unit-test-only-secret" not in output


def test_endpoint_redirect_is_not_delivery_success(delivery):
    state, calls, _ = delivery
    state["endpoint_status"] = 307
    assert seed.main(["seed"]) == 1
    assert len(calls) == 2


def test_help_does_not_need_a_secret_or_make_requests(delivery, monkeypatch):
    _, calls, _ = delivery
    monkeypatch.delenv("REBOUND_STREAM_SECRET")
    assert seed.main(["seed", "--help"]) == 0
    assert calls == []


def test_missing_secret_makes_no_requests(delivery, monkeypatch):
    _, calls, _ = delivery
    monkeypatch.delenv("REBOUND_STREAM_SECRET")
    assert seed.main(["seed"]) == 2
    assert calls == []


def test_rpc_redirect_is_not_followed_and_never_receives_stream_secret(delivery, capsys):
    state, calls, _ = delivery
    state["rpc_status"] = 307
    assert seed.main(["seed"]) == 1
    assert len(calls) == 1 and calls[0][1]["follow_redirects"] is False
    assert "x-rebound-secret" not in calls[0][1].get("headers", {})
    assert "unit-test-only-secret" not in capsys.readouterr().out
