#!/usr/bin/env python3
"""outlook_email.py — CLI tool and agent interface for Windows Outlook via PowerShell COM.

Allows agents and command-line users to search emails, draft messages (with optional modal
inspector pop-up), fetch email content by entry ID, and retrieve recent correspondence
across the WSL/Windows boundary.
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
from datetime import date, datetime


class OutlookError(Exception):
    """Raised when Outlook COM automation or PowerShell execution fails."""


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
    raise OutlookError("powershell.exe not found on PATH or standard WSL mount paths")


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
        raise OutlookError(
            f"PowerShell Outlook automation timed out after {timeout}s"
        ) from exc
    except Exception as exc:
        raise OutlookError(f"Failed to execute PowerShell process: {exc}") from exc

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise OutlookError(f"PowerShell error: {err_msg}")

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
        raise OutlookError(
            f"Invalid JSON returned from PowerShell: {raw[:200]}"
        ) from exc
    else:
        if isinstance(data, dict) and data.get("status") == "error":
            raise OutlookError(str(data.get("message", "Unknown Outlook COM error")))
        if isinstance(data, dict):
            return data
        return {"data": data}


def search_emails(
    query: str | None = None,
    sender: str | None = None,
    since: date | None = None,
    folder: int = 6,  # olFolderInbox = 6, olFolderSentMail = 5
    limit: int = 20,
    runner: Callable[[str], str] | None = None,
) -> list[dict[str, object]]:
    """Search Outlook emails in the specified folder matching criteria."""
    ps_filter_clauses = []
    if since is not None:
        since_str = since.strftime("%Y-%m-%d 00:00:00")
        ps_filter_clauses.append(
            f"@SQL=\"urn:schemas:httpmail:datereceived\" >= '{since_str}'"
        )

    filter_dasl = " AND ".join(ps_filter_clauses) if ps_filter_clauses else ""
    b64_q = _b64(query or "")
    b64_s = _b64(sender or "")
    b64_dasl = _b64(filter_dasl)

    script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$utf8 = [System.Text.Encoding]::UTF8

$q = $utf8.GetString([System.Convert]::FromBase64String('{b64_q}'))
$s = $utf8.GetString([System.Convert]::FromBase64String('{b64_s}'))
$dasl = $utf8.GetString([System.Convert]::FromBase64String('{b64_dasl}'))

try {{
    $outlook = New-Object -ComObject Outlook.Application
    $namespace = $outlook.GetNamespace('MAPI')
    $targetFolder = $namespace.GetDefaultFolder({folder})
    $items = $targetFolder.Items

    if ($dasl) {{
        $items = $items.Restrict($dasl)
    }}
    $items.Sort('[ReceivedTime]', $true)

    $results = @()
    $count = 0

    foreach ($item in $items) {{
        if ($count -ge {limit}) {{ break }}
        if ($item -is [System.__ComObject]) {{
            try {{
                $subj = $item.Subject
                $sName = $item.SenderName
                $sEmail = $item.SenderEmailAddress
                if ($item.Sender -and $item.Sender.AddressEntryUserType -eq 0) {{
                    $exUser = $item.Sender.GetExchangeUser()
                    if ($exUser -and $exUser.PrimarySmtpAddress) {{
                        $sEmail = $exUser.PrimarySmtpAddress
                    }}
                }}

                if ($q -and $subj -notlike "*$q*" -and $item.Body -notlike "*$q*") {{
                    continue
                }}
                if ($s -and $sName -notlike "*$s*" -and $sEmail -notlike "*$s*") {{
                    continue
                }}

                $preview = ''
                if ($item.Body) {{
                    $cleanBody = $item.Body -replace '\\r\\n', ' ' -replace '\\s+', ' '
                    $preview = $cleanBody.Substring(0, [Math]::Min(200, $cleanBody.Length))
                }}

                $results += [PSCustomObject]@{{
                    entry_id = $item.EntryID
                    subject = $subj
                    sender_name = $sName
                    sender_email = $sEmail
                    received_time = if ($item.ReceivedTime) {{ $item.ReceivedTime.ToString('o') }} else {{ $null }}
                    body_preview = $preview
                }}
                $count++
            }} catch {{}}
        }}
    }}

    [PSCustomObject]@{{
        status = 'success'
        count = $results.Count
        emails = $results
    }} | ConvertTo-Json -Depth 3 -Compress
}} catch {{
    [PSCustomObject]@{{
        status = 'error'
        message = $_.Exception.Message
    }} | ConvertTo-Json -Compress
}}
"""
    data = run_powershell_json(script, runner=runner)
    emails = data.get("emails", [])
    if isinstance(emails, dict):
        return [emails]
    if isinstance(emails, list):
        return [item for item in emails if isinstance(item, dict)]
    return []


def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    display: bool = True,
    runner: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Create a draft email in Outlook and optionally display inspector modal."""
    b64_to = _b64(to)
    b64_cc = _b64(cc or "")
    b64_subj = _b64(subject)
    # Normalize escaped literal \n sequences if present in body
    clean_body = body.replace("\\r\\n", "\n").replace("\\n", "\n")
    b64_body = _b64(clean_body)
    display_flag = "$true" if display else "$false"

    script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$utf8 = [System.Text.Encoding]::UTF8

$to = $utf8.GetString([System.Convert]::FromBase64String('{b64_to}'))
$cc = $utf8.GetString([System.Convert]::FromBase64String('{b64_cc}'))
$subj = $utf8.GetString([System.Convert]::FromBase64String('{b64_subj}'))
$body = $utf8.GetString([System.Convert]::FromBase64String('{b64_body}'))

try {{
    try {{
        $outlook = [System.Runtime.InteropServices.Marshal]::GetActiveObject('Outlook.Application')
    }} catch {{
        $outlook = New-Object -ComObject Outlook.Application
    }}
    $namespace = $outlook.GetNamespace('MAPI')

    $mail = $null
    for ($i = 0; $i -lt 10; $i++) {{
        try {{
            $mail = $outlook.CreateItem(0)
            break
        }} catch [System.Runtime.InteropServices.COMException] {{
            if ($_.Exception.HResult -eq -2147418111) {{
                Start-Sleep -Milliseconds 500
            }} else {{
                throw
            }}
        }}
    }}

    if ($mail -eq $null) {{
        throw 'Failed to create MailItem after retry loop'
    }}

    $mail.To = $to
    if ($cc) {{
        $mail.CC = $cc
    }}
    $mail.Subject = $subj
    $mail.Body = $body
    $mail.Save()

    if ({display_flag}) {{
        $mail.Display($false)
    }}

    [PSCustomObject]@{{
        status = 'success'
        entry_id = $mail.EntryID
        subject = $mail.Subject
    }} | ConvertTo-Json -Compress
}} catch {{
    [PSCustomObject]@{{
        status = 'error'
        message = $_.Exception.Message
    }} | ConvertTo-Json -Compress
}}
"""
    return run_powershell_json(script, runner=runner)


def get_email(
    entry_id: str,
    runner: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Retrieve detailed email content by EntryID."""
    b64_id = _b64(entry_id)
    script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$utf8 = [System.Text.Encoding]::UTF8
$id = $utf8.GetString([System.Convert]::FromBase64String('{b64_id}'))

try {{
    $outlook = New-Object -ComObject Outlook.Application
    $namespace = $outlook.GetNamespace('MAPI')
    $item = $namespace.GetItemFromID($id)

    $sEmail = $item.SenderEmailAddress
    if ($item.Sender -and $item.Sender.AddressEntryUserType -eq 0) {{
        $exUser = $item.Sender.GetExchangeUser()
        if ($exUser -and $exUser.PrimarySmtpAddress) {{
            $sEmail = $exUser.PrimarySmtpAddress
        }}
    }}

    [PSCustomObject]@{{
        status = 'success'
        entry_id = $item.EntryID
        subject = $item.Subject
        sender_name = $item.SenderName
        sender_email = $sEmail
        received_time = if ($item.ReceivedTime) {{ $item.ReceivedTime.ToString('o') }} else {{ $null }}
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


def get_recent_correspondence(
    since: date | None = None,
    limit: int = 50,
    runner: Callable[[str], str] | None = None,
) -> list[dict[str, object]]:
    """Retrieve recent emails received in Inbox."""
    return search_emails(since=since, folder=6, limit=limit, runner=runner)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for outlook_email.py."""
    parser = argparse.ArgumentParser(
        description="Outlook email tool via PowerShell COM automation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # search
    search_p = subparsers.add_parser("search", help="Search emails")
    search_p.add_argument("--query", "-q", help="Search text in subject or body")
    search_p.add_argument("--sender", "-s", help="Sender name or email")
    search_p.add_argument("--since", help="Filter since date (YYYY-MM-DD)")
    search_p.add_argument("--limit", type=int, default=20, help="Max results")
    search_p.add_argument("--json", action="store_true", help="Output JSON")

    # draft
    draft_p = subparsers.add_parser("draft", help="Create an email draft")
    draft_p.add_argument("--to", required=True, help="Recipient email")
    draft_p.add_argument("--cc", help="CC recipient email")
    draft_p.add_argument("--subject", required=True, help="Email subject")
    draft_p.add_argument("--body", required=True, help="Email body text")
    draft_p.add_argument(
        "--no-display", action="store_true", help="Do not pop modal window"
    )
    draft_p.add_argument("--json", action="store_true", help="Output JSON")

    # get
    get_p = subparsers.add_parser("get", help="Get email details by EntryID")
    get_p.add_argument("--id", required=True, help="Outlook EntryID")
    get_p.add_argument("--json", action="store_true", help="Output JSON")

    # recent
    recent_p = subparsers.add_parser("recent", help="Fetch recent correspondence")
    recent_p.add_argument("--since", help="Filter since date (YYYY-MM-DD)")
    recent_p.add_argument("--limit", type=int, default=30, help="Max results")
    recent_p.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args(argv)

    try:
        if args.command == "search":
            since_d = (
                datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None
            )
            res = search_emails(
                query=args.query,
                sender=args.sender,
                since=since_d,
                limit=args.limit,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Found {len(res)} emails:")
                for item in res:
                    print(
                        f"[{str(item.get('received_time', ''))[:16]}] {item.get('sender_name', '')} <{item.get('sender_email', '')}>: {item.get('subject', '')}"
                    )
                    print(f"  EntryID: {item.get('entry_id', '')}")
                    if item.get("body_preview"):
                        print(f"  Preview: {str(item['body_preview'])[:100]}...")
            return 0

        if args.command == "draft":
            res = create_draft(
                to=args.to,
                subject=args.subject,
                body=args.body,
                cc=args.cc,
                display=not args.no_display,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(
                    f"Draft created successfully: {res.get('subject', '')} (EntryID: {res.get('entry_id', '')})"
                )
            return 0

        if args.command == "get":
            res = get_email(entry_id=args.id)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Subject: {res.get('subject', '')}")
                print(
                    f"From: {res.get('sender_name', '')} <{res.get('sender_email', '')}>"
                )
                print(f"Date: {res.get('received_time', '')}")
                print("\n--- Body ---\n")
                print(res.get("body", ""))
            return 0

        if args.command == "recent":
            since_d = (
                datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None
            )
            res = get_recent_correspondence(since=since_d, limit=args.limit)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                for item in res:
                    print(
                        f"[{str(item.get('received_time', ''))[:16]}] {item.get('sender_name', '')}: {item.get('subject', '')}"
                    )
            return 0

    except OutlookError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "message": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
