#!/usr/bin/env python3
"""outlook_calendar.py — CLI tool and agent interface for Windows Outlook Calendar via PowerShell COM.

Allows agents and command-line users to query Outlook calendar appointments, expand recurrences,
and fetch meeting details across the WSL/Windows boundary.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta


class OutlookCalendarError(Exception):
    """Raised when Outlook Calendar COM automation or PowerShell execution fails."""


def find_powershell() -> str:
    """Locate powershell.exe or pwsh.exe on the system."""
    candidates = [
        shutil.which("powershell.exe"),
        shutil.which("pwsh.exe"),
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
        "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise OutlookCalendarError(
        "powershell.exe not found on PATH or standard WSL mount paths"
    )


def _b64(text: str) -> str:
    """Encode string as UTF-8 Base64 for safe transport to PowerShell."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def run_powershell_script(
    script: str,
    timeout: float = 20.0,
    runner: Callable[[str], str] | None = None,
) -> str:
    """Execute a PowerShell script block and return its raw stdout string."""
    if runner is not None:
        return runner(script)

    ps_bin = find_powershell()
    cmd = [
        ps_bin,
        "-NoProfile",
        "-NonInteractive",
        "-InputFormat",
        "None",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]

    try:
        proc = subprocess.run(
            cmd,
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OutlookCalendarError(
            f"PowerShell Outlook calendar automation timed out after {timeout}s"
        ) from exc
    except Exception as exc:
        raise OutlookCalendarError(
            f"Failed to execute PowerShell process: {exc}"
        ) from exc

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise OutlookCalendarError(f"PowerShell error: {err_msg}")

    return proc.stdout.strip()


def run_powershell_json(
    script: str,
    timeout: float = 20.0,
    runner: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Execute a PowerShell script and parse the returned JSON payload."""
    raw = run_powershell_script(script, timeout=timeout, runner=runner)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OutlookCalendarError(
            f"Invalid JSON returned from PowerShell: {raw[:200]}"
        ) from exc
    else:
        if isinstance(data, dict) and data.get("status") == "error":
            raise OutlookCalendarError(
                str(data.get("message", "Unknown Outlook Calendar COM error"))
            )
        if isinstance(data, dict):
            return data
        return {"data": data}


def get_calendar_events_range(
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
    runner: Callable[[str], str] | None = None,
) -> list[dict[str, object]]:
    """Query Outlook calendar appointments within a bounded date range."""
    effective_start = start_date or date.today()
    effective_end = end_date or (effective_start + timedelta(days=1))

    start_str = effective_start.strftime("%Y-%m-%d 00:00:00")
    end_str = effective_end.strftime("%Y-%m-%d 23:59:59")
    dasl_filter = (
        f"@SQL=\"urn:schemas:calendar:dtstart\" >= '{start_str}' "
        f"AND \"urn:schemas:calendar:dtstart\" <= '{end_str}'"
    )
    b64_dasl = _b64(dasl_filter)

    script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$utf8 = [System.Text.Encoding]::UTF8
$dasl = $utf8.GetString([System.Convert]::FromBase64String('{b64_dasl}'))

try {{
    $outlook = New-Object -ComObject Outlook.Application
    $namespace = $outlook.GetNamespace('MAPI')
    $calendar = $namespace.GetDefaultFolder(9)
    $items = $calendar.Items
    $items.IncludeRecurrences = $true
    $items.Sort('[Start]')

    $filtered = $items.Restrict($dasl)

    $results = @()
    $count = 0

    foreach ($item in $filtered) {{
        if ($count -ge {limit}) {{ break }}
        if ($item -is [System.__ComObject]) {{
            try {{
                $results += [PSCustomObject]@{{
                    entry_id = $item.EntryID
                    title = $item.Subject
                    start = if ($item.Start) {{ $item.Start.ToString('o') }} else {{ $null }}
                    end = if ($item.End) {{ $item.End.ToString('o') }} else {{ $null }}
                    is_recurring = [bool]$item.IsRecurring
                    location = if ($item.Location) {{ $item.Location }} else {{ '' }}
                }}
                $count++
            }} catch {{}}
        }}
    }}

    [PSCustomObject]@{{
        status = 'success'
        count = $results.Count
        events = $results
    }} | ConvertTo-Json -Depth 3 -Compress
}} catch {{
    [PSCustomObject]@{{
        status = 'error'
        message = $_.Exception.Message
    }} | ConvertTo-Json -Compress
}}
"""
    data = run_powershell_json(script, runner=runner)
    events = data.get("events", [])
    if isinstance(events, dict):
        return [events]
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)]
    return []


def get_appointment(
    entry_id: str,
    runner: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Retrieve appointment details by EntryID."""
    b64_id = _b64(entry_id)
    script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$utf8 = [System.Text.Encoding]::UTF8
$id = $utf8.GetString([System.Convert]::FromBase64String('{b64_id}'))

try {{
    $outlook = New-Object -ComObject Outlook.Application
    $namespace = $outlook.GetNamespace('MAPI')
    $item = $namespace.GetItemFromID($id)

    [PSCustomObject]@{{
        status = 'success'
        entry_id = $item.EntryID
        title = $item.Subject
        start = if ($item.Start) {{ $item.Start.ToString('o') }} else {{ $null }}
        end = if ($item.End) {{ $item.End.ToString('o') }} else {{ $null }}
        is_recurring = [bool]$item.IsRecurring
        location = if ($item.Location) {{ $item.Location }} else {{ '' }}
        body = $item.Body
    }} | ConvertTo-Json -Compress
}} catch {{
    [PSCustomObject]@{{
        status = 'error'
        message = $_.Exception.Message
    }} | ConvertTo-Json -Compress
}}
"""
    return run_powershell_json(script, runner=runner)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for outlook_calendar.py."""
    parser = argparse.ArgumentParser(
        description="Outlook Calendar tool via PowerShell COM automation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    list_p = subparsers.add_parser("list", help="List calendar events")
    list_p.add_argument("--since", help="Start date (YYYY-MM-DD)")
    list_p.add_argument("--until", help="End date (YYYY-MM-DD)")
    list_p.add_argument("--days", type=int, help="Number of days from since/today")
    list_p.add_argument("--limit", type=int, default=50, help="Max results")
    list_p.add_argument("--json", action="store_true", help="Output JSON")

    # get
    get_p = subparsers.add_parser("get", help="Get appointment details by EntryID")
    get_p.add_argument("--id", required=True, help="Outlook EntryID")
    get_p.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            start_d = (
                datetime.strptime(args.since, "%Y-%m-%d").date()
                if args.since
                else date.today()
            )
            if args.until:
                end_d = datetime.strptime(args.until, "%Y-%m-%d").date()
            elif args.days:
                end_d = start_d + timedelta(days=args.days)
            else:
                end_d = start_d

            res = get_calendar_events_range(
                start_date=start_d, end_date=end_d, limit=args.limit
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Found {len(res)} calendar events:")
                for item in res:
                    recur = " [Recurring]" if item.get("is_recurring") else ""
                    loc = f" @ {item['location']}" if item.get("location") else ""
                    print(
                        f"[{str(item.get('start', ''))[:16]} - {str(item.get('end', ''))[11:16]}] {item.get('title', '')}{loc}{recur}"
                    )
                    print(f"  EntryID: {item.get('entry_id', '')}")
            return 0

        if args.command == "get":
            res = get_appointment(entry_id=args.id)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Title: {res.get('title', '')}")
                print(f"Start: {res.get('start', '')}")
                print(f"End: {res.get('end', '')}")
                print(f"Location: {res.get('location', '')}")
                print(f"Recurring: {res.get('is_recurring', False)}")
                print("\n--- Body ---\n")
                print(res.get("body", ""))
            return 0

    except OutlookCalendarError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
