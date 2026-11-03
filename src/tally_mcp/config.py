"""Settings, all overridable through environment variables (set them in the
MCP client's `env` block)."""
import os

TALLY_URL = os.environ.get("TALLY_URL", "http://localhost:9000")
TALLY_TIMEOUT = float(os.environ.get("TALLY_TIMEOUT", "120"))

# Row caps. Tools default to DEFAULT_ROWS and never return more than MAX_ROWS
# per call, so an agent cannot pull lakhs of vouchers into its context at once.
DEFAULT_ROWS = int(os.environ.get("TALLY_MCP_DEFAULT_ROWS", "100"))
MAX_ROWS = int(os.environ.get("TALLY_MCP_MAX_ROWS", "1000"))

# Writes go through the bridge TDL (tally/TallyMCPBridge.tdl), which polls this
# folder from inside Tally. The bridge has the same path hard-coded, so change
# both together.
QUEUE_DIR = os.environ.get("TALLY_MCP_QUEUE_DIR", r"C:\TallyMCP\queue")
BRIDGE_TIMEOUT = float(os.environ.get("TALLY_MCP_BRIDGE_TIMEOUT", "30"))

# Companies the write tools may change, separated by ";". "*" (the default)
# allows any open company; "none" turns writes off; with a list, anything else
# is refused before a job reaches Tally.
WRITE_COMPANIES = [
    c.strip()
    for c in os.environ.get("TALLY_MCP_WRITE_COMPANIES", "*").split(";")
    if c.strip() and c.strip().lower() != "none"
]

# Every write appends two JSON lines here: the request with a before-snapshot
# of the voucher, then the bridge's answer with an after-snapshot.
AUDIT_LOG = os.environ.get("TALLY_MCP_AUDIT_LOG", r"C:\TallyMCP\audit.jsonl")
