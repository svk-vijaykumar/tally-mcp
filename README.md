# tally-mcp

An MCP server that gives AI agents live access to the companies open in TallyPrime on this PC, over Tally's XML/HTTP API (`localhost:9000`).

Runs on Windows, on the same PC as TallyPrime (tested on TallyPrime 7.2).

> **Write tools change live accounts.** By default the agent may alter vouchers in any open company. Set `TALLY_MCP_WRITE_COMPANIES` to a test company's name while you try it out, and keep a backup of your data.

## Setup

1. **Turn on Tally's HTTP server.** In TallyPrime: F1 (Help) > Settings > Connectivity > Client/Server configuration. Set *TallyPrime acts as* to **Both** or **Server**, and the port to **9000**.
2. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)**, which fetches Python and the server for you:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. **Add the server to your MCP client.**

   **Claude Code:**
   ```powershell
   claude mcp add tally -- uvx --from git+https://github.com/svk-vijaykumar/tally-mcp tally-mcp
   ```

   **Claude Desktop:** add this to `%APPDATA%\Claude\claude_desktop_config.json`, then restart Claude Desktop:
   ```json
   {
     "mcpServers": {
       "tally": {
         "command": "uvx",
         "args": ["--from", "git+https://github.com/svk-vijaykumar/tally-mcp", "tally-mcp"],
         "env": { "TALLY_URL": "http://localhost:9000" }
       }
     }
   }
   ```

   **Your own agents** (Agent SDK or another MCP client) start the same command over stdio.
4. **Only if you want the write tools:** download [`tally/TallyMCPBridge.tdl`](tally/TallyMCPBridge.tdl), add it in TallyPrime under F1 > TDLs & Add-ons, and restart TallyPrime. See [Write tools](#write-tools).

Read tools work after steps 1 to 3. Ask the agent to "check Tally status" to confirm it can reach Tally.

### Working on the code

```powershell
git clone https://github.com/svk-vijaykumar/tally-mcp
cd tally-mcp
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[test]"
```

To run your local copy, point the MCP client at `.venv\Scripts\python.exe` with the arguments `-m tally_mcp`.

## Tools (read-only, slice 1)

| Tool | What it returns |
|---|---|
| `get_tally_status` | Whether Tally is reachable, the release (e.g. 7.2), and the open companies with their year dates |
| `list_ledgers` / `list_stock_items` | Masters with group and closing figures. Filter by group or name; paged with `offset` |
| `get_ledger` / `get_stock_item` | Every non-empty field of one master |
| `list_vouchers` | Vouchers in a date range (type and party filters optional), oldest first, with ledger entries; paged with `cursor` |
| `get_voucher` | One voucher in full: ledger, inventory, batch and bill details |
| `run_tdl_collection` | Any TDL collection, as structured arguments or raw `<TDLMESSAGE>` XML |

Every tool takes an optional `company`. Without it, Tally uses its active company. A name that isn't open is refused before it reaches Tally: Tally crashed (memory access violation) while a garbled company name was being sent as `SVCURRENTCOMPANY`.

## Write tools

| Tool | What it does |
|---|---|
| `get_write_status` | Whether the bridge is answering, Tally's active company, and the write list |
| `swap_voucher_ledger` | Changes the ledger on one line of an existing voucher, in place (same MasterId, same amounts) |

Writes need the bridge TDL loaded in TallyPrime: add `tally\TallyMCPBridge.tdl` from this repo in F1 > TDLs & Add-ons, then restart TallyPrime. It checks `C:\TallyMCP\queue\` every 2 seconds and runs one job at a time inside Tally. Saving from an HTTP request deadlocks Tally, which is why writes go this way.

Each write:
- runs on any open company by default, but only while it is **Tally's active company**. The bridge can only change the active company, so a job for any other company is refused and nothing is sent. To limit writes to certain companies, list them in `TALLY_MCP_WRITE_COMPANIES`.
- appends a `request` line with the full voucher before the change to `C:\TallyMCP\audit.jsonl`. If that line can't be written, nothing is sent.
- reads the voucher back afterwards, adds a `done` line with the after-snapshot, and reports `verified` with any `problems`.

A job the bridge never picks up is withdrawn, so it can't run later by surprise.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `TALLY_URL` | `http://localhost:9000` | Tally's HTTP server |
| `TALLY_TIMEOUT` | `120` | Seconds per request |
| `TALLY_MCP_DEFAULT_ROWS` | `100` | Rows per page when the agent doesn't give `limit` |
| `TALLY_MCP_MAX_ROWS` | `1000` | Hard cap on rows per call |
| `TALLY_MCP_WRITE_COMPANIES` | `*` | Companies writes may change: `*` for any open company, or names separated by `;` |
| `TALLY_MCP_AUDIT_LOG` | `C:\TallyMCP\audit.jsonl` | Audit log of every write |
| `TALLY_MCP_QUEUE_DIR` | `C:\TallyMCP\queue` | Bridge job folder (also hard-coded in the bridge TDL) |
| `TALLY_MCP_BRIDGE_TIMEOUT` | `30` | Seconds to wait for a job's answer |

## Tests

```powershell
.venv\Scripts\python -m pytest -m "not live"                         # offline
$env:TALLY_TEST_COMPANY="My Company (Test)"; .venv\Scripts\python -m pytest -m live   # reads only, unless the write-test variables below are set
```

The live write test swaps one line of a throwaway voucher and back. It runs only when `TALLY_TEST_WRITE_MASTER_ID` names a voucher whose narration starts with `ZZMCP alter test`, and `TALLY_TEST_LEDGER_A` / `TALLY_TEST_LEDGER_B` name the ledger on that line and the one to swap it to. Use a test company.

## Tally behaviour this relies on (checked live on TallyPrime 7.2)

- A collection's `MAX` is applied after `FILTER` and stops reading early. `SORT` is applied after `MAX`, so it can't be used for paging.
- A Voucher collection always comes back in date order. `list_vouchers` pages with a `date:skip` cursor, so each page rereads at most one day's vouchers.
- Date filters must use `$$Date:"01-Oct-2025"` literals. `##SVFROMDATE` is empty in a plain collection export.
- `FETCH *` on vouchers leaves out the entry lists unless `AllLedgerEntries.*` and `AllInventoryEntries.*` are fetched as well.
- Exporting a single object by name works for masters, but not for a voucher by MasterID.
- Responses can contain `&#4;` (stripped before parsing) and `UDF:` tags with no namespace declaration (declared before parsing).
- Vouchers cannot be altered, cancelled or deleted over the XML API.
- `Accept Alter` inside an HTTP request (for example from a TDL function in a collection `Compute`) deadlocks the whole Tally program, and Windows shows "not responding". Saves have to run in Tally's own flow, not in an HTTP request.
