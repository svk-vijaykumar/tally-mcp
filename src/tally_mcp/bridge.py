"""Client for the write bridge (tally/TallyMCPBridge.tdl).

Saving an alteration from inside an HTTP request deadlocks TallyPrime 7.2, so
writes are queued as files instead: this module writes request.txt, the
bridge's 2-second timer runs the job in Tally's own flow and answers in
response_<id>.txt. The bridge handles one job at a time, and only in Tally's
active company.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass

from . import config


class BridgeError(Exception):
    """The job never ran, or its outcome is unknown. The message says which."""


@dataclass
class JobResult:
    ok: bool
    message: str
    seconds: float


_lock = asyncio.Lock()


def _paths() -> tuple[str, str]:
    return os.path.join(config.QUEUE_DIR, "request.tmp"), os.path.join(config.QUEUE_DIR, "request.txt")


async def run_job(op: str, *params: str, timeout: float | None = None) -> JobResult:
    """Queue one job and wait for the bridge's answer.

    Raises BridgeError when the job was never picked up (the request is then
    withdrawn, so it cannot run later by surprise) or was picked up but did not
    answer in time (outcome unknown: check the voucher before retrying).
    """
    for p in params:
        if "\n" in p or "\r" in p:
            raise BridgeError("Job parameters cannot contain line breaks.")
    timeout = config.BRIDGE_TIMEOUT if timeout is None else timeout

    async with _lock:
        tmp, req = _paths()
        if os.path.exists(req):
            raise BridgeError(
                f"Another job is already waiting in {req}. If Tally is not running the bridge, delete that file."
            )
        job_id = uuid.uuid4().hex[:12]
        os.makedirs(config.QUEUE_DIR, exist_ok=True)
        # The bridge reads one value per line. Write then rename, so it never
        # sees a half-written request.
        with open(tmp, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join([job_id, op, *params]) + "\n")
        os.replace(tmp, req)

        resp = os.path.join(config.QUEUE_DIR, f"response_{job_id}.txt")
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if os.path.exists(resp):
                try:
                    with open(resp, encoding="utf-8-sig") as f:
                        lines = f.read().splitlines()
                except OSError:
                    lines = []  # still being written
                if lines and lines[-1].strip() == "END":
                    os.remove(resp)
                    if len(lines) < 4 or lines[0].strip() != job_id:
                        raise BridgeError(f"Unreadable bridge response for job {job_id}: {lines}")
                    return JobResult(lines[1].strip() == "OK", lines[2].strip(), round(time.monotonic() - start, 1))
            await asyncio.sleep(0.25)

        try:
            os.remove(req)
        except FileNotFoundError:
            raise BridgeError(
                f"Tally picked up job {job_id} ({op}) but did not answer within {timeout:.0f}s. "
                "Its outcome is unknown: read the voucher back before retrying."
            ) from None
        raise BridgeError(
            f"The Tally bridge did not pick up the job within {timeout:.0f}s, so nothing was changed. "
            "Check TallyPrime is running with tally/TallyMCPBridge.tdl loaded (F1 > TDLs & Add-ons), "
            "then restart TallyPrime so its timer starts."
        )


async def active_company(timeout: float = 8) -> str:
    """Ping the bridge and return Tally's active company, the only one a job can change."""
    result = await run_job("PING", timeout=timeout)
    marker = "current company="
    if not result.ok or marker not in result.message:
        raise BridgeError(f"Unexpected PING answer from the bridge: {result.message!r}")
    return result.message.split(marker, 1)[1].strip()
