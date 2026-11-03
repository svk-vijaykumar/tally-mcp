import asyncio
from xml.etree import ElementTree as ET

import pytest

from tally_mcp import client, server
from tally_mcp.client import TallyError


def test_element_to_dict_drops_empties_and_lists_dot_list_children():
    el = ET.fromstring(
        '<VOUCHER NAME="x"><DATE TYPE="Date">20251001</DATE><NARRATION/>'
        "<MASTERID> 11191</MASTERID>"
        "<ALLLEDGERENTRIES.LIST><LEDGERNAME>Cash</LEDGERNAME><AMOUNT>-100.00</AMOUNT></ALLLEDGERENTRIES.LIST>"
        "<ALLLEDGERENTRIES.LIST><LEDGERNAME>Sales</LEDGERNAME><AMOUNT>100.00</AMOUNT></ALLLEDGERENTRIES.LIST>"
        "<EMPTY.LIST/></VOUCHER>"
    )
    assert client.element_to_dict(el) == {
        "name": "x",
        "date": "20251001",
        "masterid": "11191",
        "allledgerentries": [
            {"ledgername": "Cash", "amount": "-100.00"},
            {"ledgername": "Sales", "amount": "100.00"},
        ],
    }


def test_collection_elements_ignores_cmpinfo_counters():
    root = ET.fromstring(
        "<ENVELOPE><BODY><DESC><CMPINFO><VOUCHER>0</VOUCHER></CMPINFO></DESC>"
        "<DATA><COLLECTION><VOUCHER><DATE>20251001</DATE></VOUCHER></COLLECTION></DATA></BODY></ENVELOPE>"
    )
    assert [client.element_to_dict(e) for e in client.collection_elements(root)] == [{"date": "20251001"}]


def test_simple_collection_escapes_formulas_and_names():
    xml = client.simple_collection("Voucher", ["Date"], ['$Date >= $$Date:"01-Oct-2025"'], child_of="A & B", max_rows=5)
    assert "<CHILDOF>A &amp; B</CHILDOF>" in xml
    assert "$Date &gt;= $$Date:\"01-Oct-2025\"" in xml
    assert "<MAX>5</MAX>" in xml and "<FILTER>ZZMcpF0</FILTER>" in xml


def test_dates():
    assert client.parse_date("2025-10-01") == "20251001"
    assert client.tdl_date("20251001") == '$$Date:"01-Oct-2025"'
    with pytest.raises(TallyError):
        client.parse_date("2025-13-01")


def test_tdl_string_refuses_double_quote():
    with pytest.raises(TallyError):
        client.tdl_string('Say "hi"')


def test_strips_illegal_charrefs_only():
    assert client._strip_illegal_xml_charrefs("&#4;Primary&#10;&#65;") == "Primary&#10;&#65;"


def _fake_vouchers(per_day):
    """Date-ordered rows, like Tally's own Voucher collection order."""
    rows, mid = [], 1
    for day, n in per_day:
        for _ in range(n):
            rows.append({"masterid": str(mid), "date": day})
            mid += 1
    return rows


def test_list_vouchers_cursor_pages_through_everything_once(monkeypatch):
    data = _fake_vouchers([("20251001", 3), ("20251002", 5), ("20251003", 1), ("20251005", 4)])

    async def fake_export(tdl, company=None, *args):
        # Emulate the filter "$Date >= <cursor date>" and MAX from the request.
        import re

        start = re.search(r'\$\$Date:"(\d\d-\w\w\w-\d{4})"', tdl).group(1)
        from datetime import datetime

        start = datetime.strptime(start, "%d-%b-%Y").strftime("%Y%m%d")
        max_rows = int(re.search(r"<MAX>(\d+)</MAX>", tdl).group(1))
        return [r for r in data if r["date"] >= start][:max_rows]

    async def one_company(refresh=False):
        return [{"name": "Co"}]

    monkeypatch.setattr(client, "export_collection", fake_export)
    monkeypatch.setattr(server, "_open_companies", one_company)

    seen, cursor, calls = [], None, 0
    while True:
        page = asyncio.run(
            server.list_vouchers("2025-10-01", "2025-10-31", include_entries=False, limit=2, cursor=cursor)
        )
        seen += [v["master_id"] for v in page["vouchers"]]
        calls += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [r["masterid"] for r in data]
    assert calls == 7


def test_name_attribute_and_child_are_not_doubled():
    el = ET.fromstring('<COMPANY NAME="A"><NAME>A</NAME><GUID>g</GUID></COMPANY>')
    assert client.element_to_dict(el) == {"name": "A", "guid": "g"}


def test_unknown_company_is_refused_before_reaching_tally(monkeypatch):
    sent = []

    async def fake_export(tdl, company=None, *args):
        sent.append(company)
        return [{"name": "Demo Traders (F.Y 2025-26)-Test"}]

    monkeypatch.setattr(client, "export_collection", fake_export)
    monkeypatch.setattr(server, "_open_cache", None)
    assert asyncio.run(server._company("demo traders (f.y 2025-26)-test")) == "Demo Traders (F.Y 2025-26)-Test"
    with pytest.raises(server.ToolError, match="not open"):
        asyncio.run(server.list_ledgers(company="No Such Co"))
    assert "No Such Co" not in sent


def test_omitted_company_defaults_to_the_one_open(monkeypatch):
    open_names = ["Demo Traders (F.Y 2025-26)-Test"]

    async def fake_export(tdl, company=None, *args):
        return [{"name": n} for n in open_names]

    monkeypatch.setattr(client, "export_collection", fake_export)
    monkeypatch.setattr(server, "_open_cache", None)
    assert asyncio.run(server._company(None)) == "Demo Traders (F.Y 2025-26)-Test"
    open_names.append("LedgerMins Test")
    with pytest.raises(server.ToolError, match="Pass `company`"):
        asyncio.run(server._company(None))


def test_raw_tdl_unwraps_tdlmessage_and_needs_the_named_collection():
    coll = '<COLLECTION NAME="VtCount"><TYPE>Voucher</TYPE></COLLECTION>'
    assert server._raw_tdl(f"<TDLMESSAGE>{coll}</TDLMESSAGE>", "VtCount") == coll
    assert server._raw_tdl(coll, "vtcount") == coll
    with pytest.raises(server.ToolError, match="no <COLLECTION"):
        server._raw_tdl(coll, "Other")


def test_undeclared_udf_prefix_parses():
    text = (
        "<ENVELOPE><BODY><DATA><COLLECTION><VOUCHER>"
        '<UDF:VEHICLE.LIST DESC="`Vehicle`" ISLIST="YES"><UDF:VEHICLE DESC="`Vehicle`">TN01</UDF:VEHICLE></UDF:VEHICLE.LIST>'
        "</VOUCHER></COLLECTION></DATA></BODY></ENVELOPE>"
    )
    root = ET.fromstring(client._declare_prefixes(text))
    [vch] = client.collection_elements(root)
    assert client.element_to_dict(vch) == {"udf:vehicle": [{"udf:vehicle": "TN01"}]}
