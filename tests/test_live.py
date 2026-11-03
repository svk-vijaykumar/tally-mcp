"""Checks against a running TallyPrime. Run with: pytest -m live
Uses TALLY_TEST_COMPANY, or the first open company."""
import asyncio
import os

import pytest

from tally_mcp import server

pytestmark = pytest.mark.live


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def company():
    status = run(server.get_tally_status())
    names = [c["name"] for c in status["companies"]]
    assert names, "no company open in Tally"
    wanted = os.environ.get("TALLY_TEST_COMPANY")
    return wanted if wanted in names else names[0]


def test_status_reports_release():
    status = run(server.get_tally_status())
    assert status["reachable"] and status["release"]


def test_ledger_paging_matches_one_big_page(company):
    big = run(server.list_ledgers(company=company, limit=60))["rows"]
    paged, offset = [], 0
    while offset is not None and len(paged) < 60:
        page = run(server.list_ledgers(company=company, limit=25, offset=offset))
        paged += page["rows"]
        offset = page["next_offset"]
    assert [r["name"] for r in paged[:60]] == [r["name"] for r in big]


def test_get_ledger_roundtrip(company):
    name = run(server.list_ledgers(company=company, limit=1))["rows"][0]["name"]
    assert run(server.get_ledger(name, company=company))["name"] == name


def test_voucher_cursor_paging_matches_one_big_page(company):
    status = run(server.get_tally_status())
    co = next(c for c in status["companies"] if c["name"] == company)
    start = f"{co['books_from'][:4]}-{co['books_from'][4:6]}-{co['books_from'][6:]}"
    end = f"{co['ending_at'][:4]}-{co['ending_at'][4:6]}-{co['ending_at'][6:]}"

    big = run(server.list_vouchers(start, end, company=company, include_entries=False, limit=150))
    want = [v["master_id"] for v in big["vouchers"]]
    got, cursor = [], None
    while len(got) < len(want):
        page = run(server.list_vouchers(start, end, company=company, include_entries=False, limit=7, cursor=cursor))
        got += [v["master_id"] for v in page["vouchers"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert got[: len(want)] == want


def test_get_voucher_has_entries(company):
    status = run(server.get_tally_status())
    co = next(c for c in status["companies"] if c["name"] == company)
    b, e = co["books_from"], co["ending_at"]
    v = run(server.list_vouchers(f"{b[:4]}-{b[4:6]}-{b[6:]}", f"{e[:4]}-{e[4:6]}-{e[6:]}", company=company, limit=1))
    first = v["vouchers"][0]
    d = first["date"]
    full = run(server.get_voucher(int(first["master_id"]), company=company, date=f"{d[:4]}-{d[4:6]}-{d[6:]}"))
    assert full["masterid"] == first["master_id"]
    assert full.get("allledgerentries") or full.get("allinventoryentries")


def test_run_tdl_collection_structured(company):
    out = run(
        server.run_tdl_collection(
            object_type="Group", fetch=["Name", "Parent"], filters=['$Name = "Sundry Debtors"'], company=company, limit=50
        )
    )
    assert out["count"] == 1 and out["rows"][0]["name"] == "Sundry Debtors"


# The live write test swaps one line of a throwaway voucher and back. Point it at
# one you made for the purpose: a voucher whose narration starts with
# "ZZMCP alter test" and that has a line on TALLY_TEST_LEDGER_A.


def test_swap_voucher_ledger_and_back():
    company = os.environ.get("TALLY_TEST_COMPANY")
    master_id = os.environ.get("TALLY_TEST_WRITE_MASTER_ID")
    ledger_a = os.environ.get("TALLY_TEST_LEDGER_A")
    ledger_b = os.environ.get("TALLY_TEST_LEDGER_B")
    if not (company and master_id and ledger_a and ledger_b):
        pytest.skip("set TALLY_TEST_COMPANY, TALLY_TEST_WRITE_MASTER_ID, TALLY_TEST_LEDGER_A and TALLY_TEST_LEDGER_B")
    if not server._on_write_list(company):
        pytest.skip("TALLY_TEST_COMPANY is not on the write list")
    master_id = int(master_id)
    v = run(server.get_voucher(master_id, company=company))
    if not v.get("narration", "").startswith("ZZMCP alter test"):
        pytest.skip(f"MasterId {master_id} is not a ZZMCP test voucher")

    there = run(server.swap_voucher_ledger(master_id, ledger_a, ledger_b, company))
    back = run(server.swap_voucher_ledger(master_id, ledger_b, ledger_a, company))
    assert there["verified"] and back["verified"], (there["problems"], back["problems"])
    assert int(back["alter_id"]["after"]) == int(there["alter_id"]["after"]) + 1
    assert back["ledger_lines_after"] == there["ledger_lines_before"]
