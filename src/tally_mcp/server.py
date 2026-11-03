"""MCP server exposing the companies loaded in TallyPrime to AI agents.

Read tools take an optional `company`; when it is omitted Tally answers from
whichever company is currently active. Write tools go through the bridge TDL
(see bridge.py) and need `company`, which must be on the write list and be
Tally's active company.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import bridge, client, config
from .bridge import BridgeError
from .client import TallyError, parse_date, simple_collection, tdl_date, tdl_string
from .config import DEFAULT_ROWS, MAX_ROWS

mcp = MCPServer(
    name="tally",
    instructions=(
        "Live access to the companies open in TallyPrime on this PC. Start with get_tally_status to see "
        "which companies are open; pass `company` (exact name) to every other tool when more than one is open. "
        "Dates are YYYY-MM-DD. List tools are paged: keep calling with the returned next_offset / next_cursor "
        "until it is null. For anything the typed tools don't cover, use run_tdl_collection. "
        "Amounts follow Tally's sign convention: negative = debit, positive = credit. "
        "Write tools change live accounts: check get_write_status first. They work only on companies on the "
        "write list and only in Tally's active company, and every change is audited with before/after snapshots."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


def _rows(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_ROWS
    if limit < 1:
        raise ToolError("limit must be at least 1.")
    return min(limit, MAX_ROWS)


def _quoted(value: str) -> str:
    try:
        return tdl_string(value)
    except TallyError as exc:
        raise ToolError(str(exc)) from exc


def _date(value: str) -> str:
    """YYYY-MM-DD -> YYYYMMDD, as a ToolError when malformed."""
    try:
        return parse_date(value)
    except TallyError as exc:
        raise ToolError(str(exc)) from exc


async def _call(coro):
    """Run a client call, turning TallyError into a ToolError the model can read."""
    try:
        return await coro
    except TallyError as exc:
        raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------- companies

_open_cache: tuple[float, list[dict[str, Any]]] | None = None


async def _open_companies(refresh: bool = False) -> list[dict[str, Any]]:
    global _open_cache
    if not refresh and _open_cache and time.monotonic() - _open_cache[0] < 30:
        return _open_cache[1]
    rows = await _call(
        client.export_collection(simple_collection("Company", ["Name", "StartingFrom", "BooksFrom", "EndingAt", "GUID"]))
    )
    _open_cache = (time.monotonic(), rows)
    return rows


async def _company(company: str | None) -> str | None:
    """Return the open company's exact name, or raise a ToolError listing the
    open ones. Every tool goes through this before sending `company` to Tally:
    a name that is not open must never reach SVCURRENTCOMPANY. On 2026-11-03
    Tally crashed (c0000005, tallyerr.log) while a garbled company name was
    being sent as SVCURRENTCOMPANY.

    With no company given, the one open company is used. Confirmed live
    2026-11-03: without SVCURRENTCOMPANY, filtered Voucher collections came
    back empty (no error) after a Tally restart, even with one company open."""
    if company is None:
        names = [c.get("name", "") for c in await _open_companies(refresh=True)]
        if len(names) == 1:
            return names[0]
        raise ToolError(f"Pass `company`: {len(names)} companies are open in Tally: {names}")
    for refresh in (False, True):
        names = [c.get("name", "") for c in await _open_companies(refresh)]
        for name in names:
            if name.lower() == company.strip().lower():
                return name
    raise ToolError(f"Company {company!r} is not open in Tally. Open companies: {names}")


@mcp.tool(annotations=READ_ONLY)
async def get_tally_status() -> dict[str, Any]:
    """Check Tally is reachable and list the companies open in it, with their
    financial-year dates and TallyPrime's release number."""
    companies = await _open_companies(refresh=True)
    release: dict[str, str] = {}
    if companies:
        # Only an Object export's header carries the product release fields.
        root = await _call(client.send(client.object_envelope("Company", companies[0]["name"], ["Name"])))
        release = client.product_release(root)
    return {
        "reachable": True,
        "release": f"{release['prodmajorrel']}.{release['prodminorrel']}" if "prodmajorrel" in release else None,
        "companies": [
            {
                "name": c.get("name"),
                "starting_from": c.get("startingfrom"),
                "books_from": c.get("booksfrom"),
                "ending_at": c.get("endingat"),
                "guid": c.get("guid"),
            }
            for c in companies
        ],
    }


# ---------------------------------------------------------------- masters


async def _list_masters(
    object_type: str,
    fetch: list[str],
    company: str | None,
    group: str | None,
    name_contains: str | None,
    limit: int | None,
    offset: int,
) -> dict[str, Any]:
    if offset < 0:
        raise ToolError("offset must be 0 or more.")
    company = await _company(company)
    limit = _rows(limit)
    filters = [f"$Name CONTAINS {_quoted(name_contains)}"] if name_contains else []
    # MAX one past the page tells us whether another page exists. Masters run
    # to thousands, not lakhs, so offset paging is cheap enough here.
    tdl = simple_collection(object_type, fetch, filters, child_of=group, max_rows=offset + limit + 1)
    rows = await _call(client.export_collection(tdl, company))
    page = rows[offset : offset + limit]
    has_more = len(rows) > offset + limit
    return {"rows": page, "count": len(page), "next_offset": offset + limit if has_more else None}


@mcp.tool(annotations=READ_ONLY)
async def list_ledgers(
    company: str | None = None,
    group: str | None = None,
    name_contains: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """List ledger masters with parent group and closing balance.

    group: only ledgers under this group, at any depth (e.g. "Sundry Debtors").
    name_contains: case-insensitive substring match on the ledger name.
    Paged: pass the returned next_offset back as offset until it is null.
    """
    return await _list_masters(
        "Ledger", ["Name", "Parent", "ClosingBalance", "MasterId"], company, group, name_contains, limit, offset
    )


@mcp.tool(annotations=READ_ONLY)
async def list_stock_items(
    company: str | None = None,
    group: str | None = None,
    name_contains: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """List stock items with stock group, base unit, closing quantity and value.

    group: only items under this stock group, at any depth.
    name_contains: case-insensitive substring match on the item name.
    Paged: pass the returned next_offset back as offset until it is null.
    """
    return await _list_masters(
        "Stock Item",
        ["Name", "Parent", "BaseUnits", "ClosingBalance", "ClosingRate", "ClosingValue", "MasterId"],
        company,
        group,
        name_contains,
        limit,
        offset,
    )


async def _get_master(subtype: str, name: str, company: str | None) -> dict[str, Any]:
    company = await _company(company)
    root = await _call(client.send(client.object_envelope(subtype, name, ["*"], company)))
    element = client.object_element(root)
    if element is None:
        raise ToolError(f"No {subtype} named {name!r} in {'company ' + repr(company) if company else 'the active company'}.")
    return client.element_to_dict(element)


@mcp.tool(annotations=READ_ONLY)
async def get_ledger(name: str, company: str | None = None) -> dict[str, Any]:
    """Every non-empty field of one ledger master (exact name): group, balances,
    GST and bill-wise settings, addresses, MasterId, and so on."""
    return await _get_master("Ledger", name, company)


@mcp.tool(annotations=READ_ONLY)
async def get_stock_item(name: str, company: str | None = None) -> dict[str, Any]:
    """Every non-empty field of one stock item master (exact name): group,
    units, GST details, opening and closing stock, MasterId, and so on."""
    return await _get_master("Stock Item", name, company)


# ---------------------------------------------------------------- vouchers

_VOUCHER_SUMMARY_FETCH = ["MasterId", "Date", "VoucherTypeName", "VoucherNumber", "PartyLedgerName", "Narration"]
_VOUCHER_ENTRY_FETCH = ["AllLedgerEntries.LedgerName", "AllLedgerEntries.Amount"]


@mcp.tool(annotations=READ_ONLY)
async def list_vouchers(
    from_date: str,
    to_date: str,
    company: str | None = None,
    voucher_type: str | None = None,
    party: str | None = None,
    include_entries: bool = True,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List vouchers in a date range (YYYY-MM-DD, both inclusive), oldest first.

    Returns master_id, date, type, number, party and narration per voucher, plus
    its ledger entries when include_entries is true. Use get_voucher(master_id)
    for the full voucher with inventory and bill details.
    voucher_type: exact voucher type name (e.g. "Sales"). party: exact party ledger name.
    Paged: pass the returned next_cursor back as cursor (same other arguments)
    until it is null. Keep date ranges tight on large companies.
    """
    start, end = _date(from_date), _date(to_date)
    if start > end:
        raise ToolError("from_date is after to_date.")
    limit = _rows(limit)
    company = await _company(company)

    # Cursor = "<date>:<rows already returned on that date>". Confirmed live:
    # a Voucher collection comes back in date order, stable between calls, and
    # MAX stops the walk early — so each page restarts at the cursor date and
    # only re-reads that one day's earlier rows (a few dozen at most), never
    # the whole range. Offset paging would re-read everything before the page.
    skip = 0
    if cursor:
        try:
            cursor_date, skip_text = cursor.split(":")
            start, skip = parse_date(cursor_date), int(skip_text)
        except (ValueError, TallyError) as exc:
            raise ToolError(f"Bad cursor {cursor!r}; pass back next_cursor exactly as returned.") from exc

    filters = [f"$Date >= {tdl_date(start)} AND $Date <= {tdl_date(end)}"]
    if voucher_type:
        filters.append(f"$VoucherTypeName = {_quoted(voucher_type)}")
    if party:
        filters.append(f"$PartyLedgerName = {_quoted(party)}")

    fetch = _VOUCHER_SUMMARY_FETCH + (_VOUCHER_ENTRY_FETCH if include_entries else [])
    rows = await _call(
        client.export_collection(
            simple_collection("Voucher", fetch, filters, max_rows=skip + limit + 1), company
        )
    )

    page_raw = rows[skip : skip + limit]
    vouchers = [_voucher_summary(r, include_entries) for r in page_raw]
    next_cursor = None
    if len(rows) > skip + limit:
        last_date = page_raw[-1].get("date", "")
        # rows[0] is on the cursor date, so everything on last_date that we
        # have returned so far sits inside rows[:skip + limit].
        seen = sum(1 for r in rows[: skip + limit] if r.get("date") == last_date)
        next_cursor = f"{last_date}:{seen}"
    return {"vouchers": vouchers, "count": len(vouchers), "next_cursor": next_cursor}


def _voucher_summary(row: dict[str, Any], include_entries: bool) -> dict[str, Any]:
    out = {
        "master_id": row.get("masterid"),
        "date": row.get("date"),
        "voucher_type": row.get("vouchertypename"),
        "voucher_number": row.get("vouchernumber"),
        "party": row.get("partyledgername"),
        "narration": row.get("narration"),
    }
    if include_entries:
        out["ledger_entries"] = [
            {"ledger": e.get("ledgername"), "amount": e.get("amount")} for e in row.get("allledgerentries", [])
        ]
    return {k: v for k, v in out.items() if v not in (None, "")}


@mcp.tool(annotations=READ_ONLY)
async def get_voucher(master_id: int, company: str | None = None, date: str | None = None) -> dict[str, Any]:
    """Every non-empty field of one voucher, including ledger entries,
    inventory entries, batch/godown and bill allocations.

    master_id: from list_vouchers. date (YYYY-MM-DD, optional): the voucher's
    date if known — it makes the lookup much faster on large companies.
    """
    company = await _company(company)
    filters = [f"$MasterId = {int(master_id)}"]
    if date:
        filters.insert(0, f"$Date = {tdl_date(_date(date))}")
    # Confirmed live: FETCH * alone omits the entry sub-collections; they must
    # be fetched explicitly with their own wildcards.
    rows = await _call(
        client.export_collection(
            simple_collection("Voucher", ["*", "AllLedgerEntries.*", "AllInventoryEntries.*"], filters, max_rows=1),
            company,
        )
    )
    if not rows:
        raise ToolError(f"No voucher with MasterId {master_id}" + (f" on {date}." if date else "."))
    return rows[0]


# ---------------------------------------------------------------- power tool


_TDLMESSAGE_RE = re.compile(r"^\s*<TDLMESSAGE\s*>(.*)</TDLMESSAGE>\s*$", re.S | re.I)
_COLLECTION_NAME_RE = re.compile(r"<COLLECTION\b[^>]*\bNAME\s*=\s*\"([^\"]*)\"", re.I)


def _raw_tdl(raw_tdl_xml: str, collection_name: str) -> str:
    """The TDLMESSAGE body to send, or a ToolError before anything reaches
    Tally. Confirmed live 2026-11-03: when the exported collection is not
    defined (here an outer <TDLMESSAGE> was doubled, so the definition was
    ignored) Tally shows a modal "Could not find description!" dialog that
    blocks every HTTP request until someone clicks OK in the Tally window."""
    m = _TDLMESSAGE_RE.match(raw_tdl_xml)
    body = m.group(1) if m else raw_tdl_xml
    defined = [n.lower() for n in _COLLECTION_NAME_RE.findall(body)]
    if collection_name.lower() not in defined:
        raise ToolError(
            f'raw_tdl_xml has no <COLLECTION NAME="{collection_name}">; Tally would block on an error dialog. '
            f"Collections defined: {defined or 'none'}"
        )
    return body


@mcp.tool(annotations=READ_ONLY)
async def run_tdl_collection(
    object_type: str | None = None,
    fetch: list[str] | None = None,
    filters: list[str] | None = None,
    child_of: str | None = None,
    company: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int | None = None,
    raw_tdl_xml: str | None = None,
    collection_name: str | None = None,
) -> dict[str, Any]:
    """Run any TDL collection against Tally and return its objects as rows —
    for data the typed tools don't cover (groups, cost centres, godowns,
    batches, GST details, aggregated totals...).

    Structured mode (recommended): object_type is a Tally object type
    ("Ledger", "Group", "Stock Item", "Voucher", "Godown", "Cost Centre"...);
    fetch lists method names ("Name", "Parent", "AllLedgerEntries.Amount");
    filters are TDL boolean formulas, all of which must hold
    ('$Parent = "Sundry Debtors"', '$ClosingBalance < 0'). Compare dates with
    $$Date:"01-Apr-2025", not ##SVFROMDATE. `IN (...)` matches nothing; chain
    `=` comparisons with OR instead. child_of restricts to one parent.
    from_date / to_date (YYYY-MM-DD) set the reporting period that
    period-dependent methods such as ClosingBalance use.

    Raw mode: raw_tdl_xml is the full <TDLMESSAGE> body (COLLECTION plus any
    SYSTEM Formulae / extra definitions) and collection_name the collection to
    export. limit is then applied after Tally returns, so add <MAX> yourself
    on big collections.
    """
    limit = _rows(limit)
    company = await _company(company)
    static_variables: dict[str, str] = {}
    if from_date:
        static_variables["SVFROMDATE"] = _date(from_date)
    if to_date:
        static_variables["SVTODATE"] = _date(to_date)

    if raw_tdl_xml:
        if not collection_name:
            raise ToolError("collection_name is required with raw_tdl_xml.")
        tdl, name = _raw_tdl(raw_tdl_xml, collection_name), collection_name
    else:
        if not object_type or not fetch:
            raise ToolError("Give object_type and fetch (or raw_tdl_xml and collection_name).")
        tdl = simple_collection(object_type, fetch, filters, child_of=child_of, max_rows=limit + 1)
        name = "ZZMcpColl"

    rows = await _call(client.export_collection(tdl, company, name, static_variables))
    return {"rows": rows[:limit], "count": min(len(rows), limit), "truncated": len(rows) > limit}


# ---------------------------------------------------------------- writes


def _audit(record: dict[str, Any]) -> None:
    """Append one JSON line to the audit log. Raises OSError when it can't, so
    a write is never sent to Tally without its before-snapshot on disk."""
    os.makedirs(os.path.dirname(config.AUDIT_LOG) or ".", exist_ok=True)
    line = json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **record}, ensure_ascii=False)
    with open(config.AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _on_write_list(company: str) -> bool:
    return "*" in config.WRITE_COMPANIES or company.lower() in (c.lower() for c in config.WRITE_COMPANIES)


async def _write_company(company: str) -> str:
    """The exact name of a company a write may change, or a ToolError saying why not."""
    name = await _company(company)
    if not _on_write_list(name):
        raise ToolError(
            f"Company {name!r} is not on the write list {config.WRITE_COMPANIES}. "
            "The user can add it via TALLY_MCP_WRITE_COMPANIES in the MCP server's settings."
        )
    try:
        active = await bridge.active_company()
    except BridgeError as exc:
        raise ToolError(str(exc)) from exc
    if active.lower() != name.lower():
        raise ToolError(
            f"Tally's active company is {active!r}. Writes run only in the active company, so ask the user to "
            f"select {name!r} in TallyPrime (F3), then try again. Nothing was changed."
        )
    return name


def _ledger_lines(voucher: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e.get("ledgername", ""), e.get("amount", "")) for e in voucher.get("allledgerentries", [])]


@mcp.tool(annotations=READ_ONLY)
async def get_write_status() -> dict[str, Any]:
    """Whether writes can run now: the bridge TDL is answering inside Tally,
    which company is active there (the only one a write can change), and which
    companies are on the write list."""
    try:
        active, error = await bridge.active_company(), None
    except BridgeError as exc:
        active, error = None, str(exc)
    return {
        "bridge_running": error is None,
        "active_company": active,
        "write_companies": config.WRITE_COMPANIES,
        "writes_possible_now": active is not None and _on_write_list(active),
        "audit_log": config.AUDIT_LOG,
        **({"error": error} if error else {}),
    }


@mcp.tool(annotations=WRITE)
async def swap_voucher_ledger(
    master_id: int, from_ledger: str, to_ledger: str, company: str, date: str | None = None
) -> dict[str, Any]:
    """Change the ledger on one line of an existing voucher, in place (same
    MasterId, same amounts). For example, move a bank entry off a Suspense ledger to
    the right party or expense ledger.

    from_ledger must be on exactly one line of the voucher; to_ledger must
    already exist. company must be on the write list and be Tally's active
    company (see get_write_status). date (YYYY-MM-DD, optional) speeds up the
    voucher lookup. The voucher is snapshotted before and after and both go to
    the audit log; the result says whether the read-back matched.
    """
    company = await _write_company(company)
    from_ledger, to_ledger = from_ledger.strip(), to_ledger.strip()
    if not from_ledger or not to_ledger:
        raise ToolError("from_ledger and to_ledger must not be empty.")
    if from_ledger.lower() == to_ledger.lower():
        raise ToolError("from_ledger and to_ledger are the same ledger.")

    before = await get_voucher(master_id, company=company, date=date)
    lines = _ledger_lines(before)
    hits = [i for i, (ledger, _) in enumerate(lines) if ledger.lower() == from_ledger.lower()]
    if len(hits) != 1:
        raise ToolError(
            f"{from_ledger!r} is on {len(hits)} lines of voucher {master_id}; this tool changes exactly one. "
            f"Its ledger lines: {[ledger for ledger, _ in lines]}. Nothing was changed."
        )

    audit_id = uuid.uuid4().hex[:12]
    try:
        _audit({"audit_id": audit_id, "event": "request", "tool": "swap_voucher_ledger", "company": company,
                "master_id": master_id, "from_ledger": from_ledger, "to_ledger": to_ledger, "before": before})
    except OSError as exc:
        raise ToolError(f"Could not write the audit log {config.AUDIT_LOG} ({exc}); nothing was changed.") from exc

    try:
        result = await bridge.run_job("SWAPLEDGER", company, str(master_id), from_ledger, to_ledger)
    except BridgeError as exc:
        _audit({"audit_id": audit_id, "event": "bridge_error", "error": str(exc)})
        raise ToolError(str(exc)) from exc
    if not result.ok:
        _audit({"audit_id": audit_id, "event": "refused", "message": result.message})
        raise ToolError(f"Tally refused the change and saved nothing: {result.message}")

    # Read back: that one line should now carry to_ledger, everything else unchanged.
    problems: list[str] = []
    after: dict[str, Any] = {}
    d = before.get("date", "")
    try:
        after = await get_voucher(master_id, company=company, date=f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else None)
    except ToolError as exc:
        problems.append(f"Could not read the voucher back: {exc}")
    if after:
        expected = [(to_ledger if i == hits[0] else ledger, amount) for i, (ledger, amount) in enumerate(lines)]
        got = _ledger_lines(after)
        if [(l.lower(), a) for l, a in got] != [(l.lower(), a) for l, a in expected]:
            problems.append(f"Ledger lines after the change are {got}, expected {expected}.")
        if int(after.get("alterid", 0)) <= int(before.get("alterid", 0)):
            problems.append("AlterId did not increase.")
    _audit({"audit_id": audit_id, "event": "done", "message": result.message,
            "verified": not problems, "problems": problems, "after": after})

    return {
        "changed": True,
        "verified": not problems,
        "problems": problems,
        "message": result.message,
        "master_id": master_id,
        "voucher_number": before.get("vouchernumber"),
        "date": d,
        "ledger_lines_before": [{"ledger": l, "amount": a} for l, a in lines],
        "ledger_lines_after": [{"ledger": l, "amount": a} for l, a in _ledger_lines(after)],
        "alter_id": {"before": before.get("alterid"), "after": after.get("alterid")},
        "seconds": result.seconds,
        "audit_id": audit_id,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
