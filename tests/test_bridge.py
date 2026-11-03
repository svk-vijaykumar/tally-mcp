"""Write path without Tally: a fake bridge answers the queue folder the way
tally/TallyMCPBridge.tdl does."""
import asyncio
import json
import os

import pytest

from tally_mcp import bridge, config, server
from tally_mcp.bridge import BridgeError

CO = "Demo Traders (F.Y 2025-26)-Test"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.setattr(config, "AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(config, "WRITE_COMPANIES", [CO])
    os.makedirs(config.QUEUE_DIR)
    return tmp_path


async def _fake_bridge(answer, jobs):
    """Serve request.txt like the bridge: read, delete, answer. answer(op, params) -> (status, message)."""
    req = os.path.join(config.QUEUE_DIR, "request.txt")
    while True:
        if os.path.exists(req):
            with open(req, encoding="utf-8") as f:
                job_id, op, *params = f.read().splitlines()
            os.remove(req)
            jobs.append((op, params))
            status, message = answer(op, params)
            with open(os.path.join(config.QUEUE_DIR, f"response_{job_id}.txt"), "w", encoding="utf-8-sig") as f:
                f.write(f"{job_id}\n{status}\n{message}\nEND\n")
        await asyncio.sleep(0.05)


def _with_bridge(answer, coro_fn):
    jobs = []

    async def main():
        task = asyncio.create_task(_fake_bridge(answer, jobs))
        try:
            return await coro_fn()
        finally:
            task.cancel()

    return asyncio.run(main()), jobs


def _ping_ok(active=CO):
    def answer(op, params):
        if op == "PING":
            return "OK", f"pong; current company={active}"
        return "OK", f"MasterID {params[1]} line 2: '{params[2]}' -> '{params[3]}'"

    return answer


def test_run_job_roundtrip(queue):
    result, jobs = _with_bridge(_ping_ok(), lambda: bridge.run_job("PING"))
    assert result.ok and result.message.endswith(CO)
    assert jobs == [("PING", [])]
    assert os.listdir(config.QUEUE_DIR) == []


def test_unanswered_job_is_withdrawn(queue):
    with pytest.raises(BridgeError, match="did not pick up"):
        asyncio.run(bridge.run_job("PING", timeout=0.5))
    assert os.listdir(config.QUEUE_DIR) == []  # cannot run later by surprise


def test_pending_request_and_line_breaks_refused(queue):
    with pytest.raises(BridgeError, match="line breaks"):
        asyncio.run(bridge.run_job("SWAPLEDGER", "a\nb"))
    open(os.path.join(config.QUEUE_DIR, "request.txt"), "w").close()
    with pytest.raises(BridgeError, match="already waiting"):
        asyncio.run(bridge.run_job("PING", timeout=0.5))


def _fake_tally(monkeypatch, alter_ids=("100", "101"), ledger_after="Accounting Charges"):
    async def companies(refresh=False):
        return [{"name": CO}, {"name": "Other Co"}]

    calls = []

    async def get_voucher(master_id, company=None, date=None):
        n = len(calls)
        calls.append(date)
        second = ledger_after if n else "Bank Suspense"
        return {
            "masterid": str(master_id), "date": "20260331", "alterid": alter_ids[min(n, 1)], "vouchernumber": "40",
            "allledgerentries": [
                {"ledgername": "Bank Charges", "amount": "-1.00"},
                {"ledgername": second, "amount": "1.00"},
            ],
        }

    monkeypatch.setattr(server, "_open_companies", companies)
    monkeypatch.setattr(server, "get_voucher", get_voucher)
    return calls


def _audit_lines():
    with open(config.AUDIT_LOG, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_swap_happy_path_is_audited_and_verified(queue, monkeypatch):
    calls = _fake_tally(monkeypatch)
    out, jobs = _with_bridge(
        _ping_ok(),
        lambda: server.swap_voucher_ledger(11310, "bank suspense", "Accounting Charges", company=CO.lower()),
    )
    assert out["verified"] and out["alter_id"] == {"before": "100", "after": "101"}
    assert jobs[1] == ("SWAPLEDGER", [CO, "11310", "bank suspense", "Accounting Charges"])
    assert calls == [None, "2026-03-31"]  # read back by date
    request, done = _audit_lines()
    assert request["event"] == "request" and request["before"]["alterid"] == "100"
    assert done["event"] == "done" and done["verified"] and done["after"]["alterid"] == "101"
    assert request["audit_id"] == done["audit_id"] == out["audit_id"]


def test_swap_reports_failed_read_back(queue, monkeypatch):
    _fake_tally(monkeypatch, alter_ids=("100", "100"), ledger_after="Bank Suspense")
    out, _ = _with_bridge(
        _ping_ok(), lambda: server.swap_voucher_ledger(11310, "Bank Suspense", "Accounting Charges", CO)
    )
    assert out["changed"] and not out["verified"] and len(out["problems"]) == 2


@pytest.mark.parametrize(
    "company, active, from_ledger, match",
    [
        ("Other Co", CO, "Bank Suspense", "not on the write list"),
        (CO, "Other Co", "Bank Suspense", "active company is 'Other Co'"),
        (CO, CO, "Cash", "is on 0 lines"),
    ],
)
def test_swap_refusals_never_send_a_write_job(queue, monkeypatch, company, active, from_ledger, match):
    _fake_tally(monkeypatch)

    async def attempt():
        with pytest.raises(server.ToolError, match=match):
            await server.swap_voucher_ledger(11310, from_ledger, "Accounting Charges", company)

    _, jobs = _with_bridge(_ping_ok(active), attempt)
    assert [op for op, _ in jobs if op != "PING"] == []
    assert not os.path.exists(config.AUDIT_LOG)


def test_star_allows_any_company(monkeypatch):
    monkeypatch.setattr(config, "WRITE_COMPANIES", ["*"])
    assert server._on_write_list("Any Company Ltd")
    monkeypatch.setattr(config, "WRITE_COMPANIES", [CO])
    assert not server._on_write_list("Any Company Ltd") and server._on_write_list(CO.upper())


def test_swap_bridge_refusal_is_audited(queue, monkeypatch):
    _fake_tally(monkeypatch)

    def answer(op, params):
        return ("OK", f"pong; current company={CO}") if op == "PING" else ("ERROR", "Ledger 'Nope' does not exist")

    async def attempt():
        with pytest.raises(server.ToolError, match="saved nothing: Ledger 'Nope'"):
            await server.swap_voucher_ledger(11310, "Bank Suspense", "Nope", CO)

    _with_bridge(answer, attempt)
    assert [r["event"] for r in _audit_lines()] == ["request", "refused"]
