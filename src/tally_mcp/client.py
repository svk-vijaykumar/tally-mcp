"""Async client for TallyPrime's XML/HTTP API.

Every behaviour noted as "confirmed live" was checked against TallyPrime 7.2;
don't "simplify" those parts without re-testing against a running Tally.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from .config import TALLY_TIMEOUT, TALLY_URL


class TallyError(Exception):
    """Tally was unreachable, or rejected / failed the request."""


# ---------------------------------------------------------------- escaping


def xml_text(value: Any) -> str:
    """Escape for XML element text."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def xml_attr(value: Any) -> str:
    """Escape for a double-quoted XML attribute."""
    return xml_text(value).replace('"', "&quot;")


def tdl_string(value: str) -> str:
    """A TDL string literal for use inside a formula. TDL has no escape for a
    double quote inside a literal, so names containing one are refused rather
    than silently matching the wrong thing."""
    if '"' in value:
        raise TallyError(f'Value {value!r} contains a double quote, which cannot be used in a TDL filter.')
    return f'"{value}"'


def parse_date(value: str) -> str:
    """'2025-10-01' or '20251001' -> '20251001'."""
    digits = value.replace("-", "").strip()
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError as exc:
        raise TallyError(f"Bad date {value!r}; use YYYY-MM-DD.") from exc
    return digits


def tdl_date(yyyymmdd: str) -> str:
    """'20251001' -> '$$Date:"01-Oct-2025"'.

    Confirmed live: filtering a bare Collection export with
    `$Date >= ##SVFROMDATE` filters nothing (the SV variables are only
    populated for report exports), while a `$$Date:"..."` literal works.
    """
    return '$$Date:"' + datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%d-%b-%Y") + '"'


_CHARREF_RE = re.compile(r"&#(x?[0-9A-Fa-f]+);")


def _strip_illegal_xml_charrefs(text: str) -> str:
    """Tally writes some reserved names (the 'Primary' group, 'Profit & Loss
    A/c', ...) with a literal 0x04, serialised as `&#4;` — a reference to a
    character XML 1.0 forbids, so ElementTree rejects the whole document.
    Confirmed live on unfiltered Ledger/Group/Godown/VoucherType exports."""

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1)
        codepoint = int(raw[1:], 16) if raw[:1].lower() == "x" else int(raw)
        if codepoint in (0x09, 0x0A, 0x0D) or codepoint >= 0x20:
            return match.group(0)
        return ""

    return _CHARREF_RE.sub(repl, text)


_PREFIX_RE = re.compile(r"</?([A-Za-z_][\w.-]*):")


def _declare_prefixes(text: str) -> str:
    """Tally writes UDF values as `<UDF:FIELD.LIST ...>` without declaring
    the UDF namespace — confirmed live: a voucher export carrying UDFs fails
    with 'unbound prefix'. Declare every prefix used, on the root, with the
    prefix itself as the URI so element_to_dict can turn it back into
    'udf:field'."""
    prefixes = sorted(set(_PREFIX_RE.findall(text)) - {"xml"})
    if not prefixes:
        return text
    declarations = "".join(f' xmlns:{p}="{p}"' for p in prefixes)
    return re.sub(r"<ENVELOPE\b", "<ENVELOPE" + declarations, text, count=1)


# ---------------------------------------------------------------- envelopes


def _static_variables(company: str | None, extra: dict[str, str] | None = None) -> str:
    sv = {"SVEXPORTFORMAT": "$$SysName:XML"}
    # No SVCURRENTCOMPANY means Tally answers from its currently active company.
    if company:
        sv["SVCURRENTCOMPANY"] = xml_text(company)
    sv.update(extra or {})
    return "".join(f"<{k}>{v}</{k}>" for k, v in sv.items())


def collection_envelope(
    collection_name: str,
    tdlmessage_xml: str,
    company: str | None = None,
    static_variables: dict[str, str] | None = None,
) -> str:
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{xml_text(collection_name)}</ID></HEADER>"
        f"<BODY><DESC><STATICVARIABLES>{_static_variables(company, static_variables)}</STATICVARIABLES>"
        f"<TDL><TDLMESSAGE>{tdlmessage_xml}</TDLMESSAGE></TDL>"
        "</DESC></BODY></ENVELOPE>"
    )


def object_envelope(subtype: str, name: str, fetch: list[str], company: str | None = None) -> str:
    """Export one master by name. Confirmed live for Ledger and Company; it
    does NOT work for a Voucher by MasterID (empty response), use a filtered
    collection for vouchers instead."""
    fetch_xml = "".join(f"<FETCH>{xml_text(f)}</FETCH>" for f in fetch)
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Object</TYPE><SUBTYPE>{xml_text(subtype)}</SUBTYPE>"
        f'<ID TYPE="Name">{xml_text(name)}</ID></HEADER>'
        f"<BODY><DESC><STATICVARIABLES>{_static_variables(company)}</STATICVARIABLES>"
        f"<FETCHLIST>{fetch_xml}</FETCHLIST></DESC></BODY></ENVELOPE>"
    )


def simple_collection(
    object_type: str,
    fetch: list[str],
    filters: list[str] | None = None,
    child_of: str | None = None,
    max_rows: int | None = None,
    name: str = "ZZMcpColl",
) -> str:
    """TDLMESSAGE body for one collection plus its filter formulas.

    Confirmed live: MAX is applied AFTER Filter and stops the walk early (5
    rows of a 2,516-voucher company in 0.5s vs 5.2s for all), but SORT is
    applied AFTER Max — it only reorders the rows already gathered, so it is
    useless for paging and is not offered here.
    """
    filters = filters or []
    formula_names = [f"ZZMcpF{i}" for i in range(len(filters))]
    return (
        f'<COLLECTION NAME="{xml_attr(name)}" ISMODIFY="No">'
        f"<TYPE>{xml_text(object_type)}</TYPE>"
        + (f"<CHILDOF>{xml_text(child_of)}</CHILDOF>" if child_of else "")
        + "".join(f"<FETCH>{xml_text(f)}</FETCH>" for f in fetch)
        + (f"<FILTER>{','.join(formula_names)}</FILTER>" if filters else "")
        + (f"<MAX>{int(max_rows)}</MAX>" if max_rows else "")
        + "</COLLECTION>"
        + "".join(
            f'<SYSTEM TYPE="Formulae" NAME="{n}">{xml_text(expr)}</SYSTEM>'
            for n, expr in zip(formula_names, filters)
        )
    )


# ---------------------------------------------------------------- transport


async def send(xml_body: str) -> ET.Element:
    """POST an envelope and return the parsed root, raising TallyError on
    connection failure, unparseable output, or a LINEERROR/ERRORMSG."""
    try:
        async with httpx.AsyncClient(timeout=TALLY_TIMEOUT) as client:
            resp = await client.post(
                TALLY_URL, content=xml_body.encode("utf-8"), headers={"Content-Type": "text/xml"}
            )
    except httpx.TimeoutException as exc:
        raise TallyError(
            f"Tally at {TALLY_URL} did not answer within {TALLY_TIMEOUT:.0f}s. "
            "An open dialog in the Tally window blocks HTTP requests; otherwise narrow the date range or filters."
        ) from exc
    except httpx.RequestError as exc:
        raise TallyError(
            f"Could not reach Tally at {TALLY_URL} ({exc}). Is TallyPrime running with the HTTP server enabled?"
        ) from exc

    text = resp.text.strip()
    if not text:
        raise TallyError("Empty response from Tally.")
    try:
        root = ET.fromstring(_declare_prefixes(_strip_illegal_xml_charrefs(text)))
    except ET.ParseError as exc:
        raise TallyError(f"Non-XML response from Tally: {text[:300]}") from exc

    error = root.findtext(".//LINEERROR") or root.findtext(".//ERRORMSG")
    if error:
        raise TallyError(error.strip())
    return root


# ---------------------------------------------------------------- parsing


def element_to_dict(el: ET.Element) -> dict[str, Any]:
    """Convert a Tally object element to a plain dict.

    Keys are the lower-cased tag names; a `.LIST` child becomes a list under
    the name without the suffix (`ALLLEDGERENTRIES.LIST` -> `allledgerentries`).
    Empty values are dropped — a full voucher is ~250 mostly-empty fields, and
    dropping them is what keeps it readable in an agent's context.
    """
    out: dict[str, Any] = {}
    if el.get("NAME"):
        out["name"] = el.get("NAME")
    for child in el:
        # '{UDF}FIELD' (see _declare_prefixes) -> 'udf:field'
        tag = re.sub(r"^\{(.*?)\}", r"\1:", child.tag).lower()
        if tag.endswith(".list"):
            value: Any = element_to_dict(child)
            if not value:
                continue
            out.setdefault(tag[: -len(".list")], []).append(value)
            continue
        if len(child):
            value = element_to_dict(child)
            if not value:
                continue
        else:
            value = (child.text or "").strip()
            if value == "":
                continue
            # Masters repeat their name as both the NAME attribute and a
            # <NAME> child; keep one, or "name" turns into a two-item list.
            if tag == "name" and value == out.get("name"):
                continue
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(value)
        else:
            out[tag] = value
    return out


def collection_elements(root: ET.Element) -> list[ET.Element]:
    """The object elements of a collection export.

    Scoped to DATA/COLLECTION on purpose: the response header's CMPINFO block
    has zero-filled <VOUCHER>, <LEDGER>... counters that a bare
    root.iter("VOUCHER") would pick up as phantom empty rows.
    """
    collection = root.find(".//DATA/COLLECTION")
    return list(collection) if collection is not None else []


def object_element(root: ET.Element) -> ET.Element | None:
    """The single object of an Object export (under DATA/TALLYMESSAGE)."""
    message = root.find(".//DATA/TALLYMESSAGE")
    if message is None or not len(message):
        return None
    return message[0]


def product_release(root: ET.Element) -> dict[str, str]:
    """Version fields from an Object export's HEADER (a Collection export's
    header does not carry them). On TallyPrime 7.2 these read
    PRODMAJORREL=7, PRODMINORREL=2."""
    header = root.find("HEADER")
    if header is None:
        return {}
    return {child.tag.lower(): (child.text or "").strip() for child in header if child.tag.startswith("PROD")}


async def export_collection(
    tdlmessage_xml: str,
    company: str | None = None,
    collection_name: str = "ZZMcpColl",
    static_variables: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    root = await send(collection_envelope(collection_name, tdlmessage_xml, company, static_variables))
    return [element_to_dict(el) for el in collection_elements(root)]
