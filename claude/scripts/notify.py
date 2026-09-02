#!/usr/bin/env python3
"""Cross-platform agent notification dispatcher.

Dispatches toast notifications across platforms (WSL/Windows, macOS, Linux)
with harness-specific branding icons and emits OSC 777 escape sequences to active
terminal sessions.

Usage:
    notify.py [message] [flags]

Flags:
    --title, -t     Notification title (default: "Agent Notification")
    --message, -m   Notification body text (or passed as positional argument)
    --harness, -H   Originating harness name (e.g. Claude, Pi, AGY, OpenCode, Copilot)
    --icon, -i      Path or name of custom icon
    --urgency, -u   Urgency level: low, normal, critical (default: normal)
    --type          Event type: completed, waiting_for_input, error (default: completed)
    --quiet, -q     Suppress non-essential output
    --verbose, -v   Emit extra diagnostic messages to stderr
"""

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import cli_common

ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"

APP_REGISTRATIONS = {
    "claude": {"id": "Agent.Claude", "name": "Claude Code", "icon": "claude.png"},
    "agy": {"id": "Agent.AGY", "name": "Antigravity (AGY)", "icon": "agy.png"},
    "gemini": {"id": "Agent.AGY", "name": "Antigravity (AGY)", "icon": "agy.png"},
    "antigravity": {"id": "Agent.AGY", "name": "Antigravity (AGY)", "icon": "agy.png"},
    "pi": {"id": "Agent.Pi", "name": "Pi Coding Agent", "icon": "pi.png"},
    "copilot": {"id": "Agent.Copilot", "name": "GitHub Copilot", "icon": "copilot.png"},
    "opencode": {"id": "Agent.OpenCode", "name": "OpenCode", "icon": "opencode.png"},
}


def is_wsl() -> bool:
    """Detect whether running inside Windows Subsystem for Linux."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        version = Path("/proc/version").read_text()
    except (OSError, UnicodeDecodeError):
        return False
    else:
        return "microsoft" in version.lower() or "wsl" in version.lower()


def get_harness_icon(
    harness: str | None, custom_icon: str | None = None
) -> Path | None:
    """Resolve the icon file path for a given harness."""
    if custom_icon:
        custom_path = Path(custom_icon).expanduser()
        if custom_path.exists():
            return custom_path

    if not harness:
        return None

    h = harness.lower().strip()
    config = APP_REGISTRATIONS.get(h)
    candidate_names = [config["icon"]] if config else [f"{h}.png"]

    for name in candidate_names:
        icon_path = ICONS_DIR / name
        if icon_path.exists():
            return icon_path
        user_icon = Path.home() / ".claude" / "icons" / name
        if user_icon.exists():
            return user_icon

    return None


def _get_windows_icons_cache() -> tuple[Path, str] | None:
    """Resolve Linux and Windows paths for the Windows AppData icons cache."""
    users_dir = Path("/mnt/c/Users")
    if not users_dir.exists():
        return None

    for user_entry in users_dir.iterdir():
        if user_entry.is_dir() and user_entry.name not in (
            "Default",
            "Default User",
            "Public",
            "All Users",
        ):
            local_appdata = user_entry / "AppData" / "Local" / "dotfiles" / "icons"
            win_path = f"C:\\Users\\{user_entry.name}\\AppData\\Local\\dotfiles\\icons"
            try:
                local_appdata.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            else:
                return local_appdata, win_path
    return None


def sync_icons_to_windows() -> tuple[Path, str] | None:
    """Ensure all harness icons are synced to Windows AppData for native WinRT access."""
    cache = _get_windows_icons_cache()
    if not cache:
        return None

    linux_cache_dir, win_cache_dir = cache
    if ICONS_DIR.exists():
        for icon_file in ICONS_DIR.glob("*.png"):
            dest = linux_cache_dir / icon_file.name
            if not dest.exists() or dest.stat().st_mtime < icon_file.stat().st_mtime:
                with contextlib.suppress(OSError):
                    shutil.copy2(icon_file, dest)
    return cache


def _escape_xml(s: str) -> str:
    """Escape special XML characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _escape_powershell_string(s: str) -> str:
    """Escape a string for use in single-quoted PowerShell literals."""
    return s.replace("'", "''")


def send_wsl_toast(
    title: str,
    message: str,
    harness: str | None = None,
    icon_path: Path | None = None,
    verbose: bool = False,
) -> bool:
    """Send a native Windows Toast notification from WSL via powershell.exe with branding."""
    powershell = (
        shutil.which("powershell.exe")
        or "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    if not os.path.exists(powershell) and not shutil.which(powershell):
        if verbose:
            sys.stderr.write("notify: powershell.exe not found for WSL toast\n")
        return False

    h_key = (harness or "").lower().strip()
    reg_info = APP_REGISTRATIONS.get(
        h_key, {"id": "Agent.Default", "name": "AI Agent", "icon": "claude.png"}
    )
    app_id = reg_info["id"]
    display_name = reg_info["name"]

    cache = sync_icons_to_windows()
    win_icon_path = ""
    if cache:
        _, win_cache_dir = cache
        icon_name = icon_path.name if icon_path else reg_info["icon"]
        win_icon_path = f"{win_cache_dir}\\{icon_name}"

    safe_title = _escape_xml(title)
    safe_msg = _escape_xml(message)

    image_tag = ""
    if win_icon_path:
        safe_icon = _escape_xml(win_icon_path)
        image_tag = (
            f'<image placement="appLogoOverride" hint-crop="circle" src="{safe_icon}"/>'
        )

    xml_content = (
        "<toast>"
        "<visual>"
        '<binding template="ToastGeneric">'
        f"<text>{safe_title}</text>"
        f"<text>{safe_msg}</text>"
        f"{image_tag}"
        "</binding>"
        "</visual>"
        "</toast>"
    )
    safe_xml = _escape_powershell_string(xml_content)
    safe_app_id = _escape_powershell_string(app_id)
    safe_app_name = _escape_powershell_string(display_name)
    safe_win_icon = _escape_powershell_string(win_icon_path)

    reg_script = (
        f"$regPath = 'HKCU:\\Software\\Classes\\AppUserModelId\\{safe_app_id}'; "
        "if (!(Test-Path $regPath)) { "
        f"New-Item -Path $regPath -Force | Out-Null; "
        f"Set-ItemProperty -Path $regPath -Name 'DisplayName' -Value '{safe_app_name}' -Type String -Force | Out-Null; "
        f"Set-ItemProperty -Path $regPath -Name 'IconUri' -Value '{safe_win_icon}' -Type String -Force | Out-Null; "
        "} "
    )

    ps_script = (
        f"{reg_script}"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
        "$toastXml = [Windows.Data.Xml.Dom.XmlDocument]::new(); "
        f"$toastXml.LoadXml('{safe_xml}'); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($toastXml); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{safe_app_id}').Show($toast)"
    )

    try:
        subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-InputFormat",
                "None",
                "-Command",
                ps_script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        if verbose:
            sys.stderr.write(f"notify: WSL toast delivery failed: {e}\n")
        return False
    else:
        return True


def send_macos_notification(title: str, message: str, verbose: bool = False) -> bool:
    """Send a macOS desktop notification via osascript."""
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        if verbose:
            sys.stderr.write(f"notify: macOS notification failed: {e}\n")
        return False
    else:
        return True


def send_linux_notification(
    title: str,
    message: str,
    icon_path: Path | None = None,
    urgency: str = "normal",
    verbose: bool = False,
) -> bool:
    """Send a desktop notification on native Linux via notify-send."""
    if not shutil.which("notify-send"):
        if verbose:
            sys.stderr.write("notify: notify-send not found on PATH\n")
        return False

    cmd = ["notify-send", "-u", urgency]
    if icon_path and icon_path.exists():
        cmd.extend(["-i", str(icon_path)])
    cmd.extend([title, message])

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        if verbose:
            sys.stderr.write(f"notify: notify-send failed: {e}\n")
        return False
    else:
        return res.returncode == 0


def send_terminal_osc(title: str, message: str, verbose: bool = False) -> bool:
    """Emit OSC 777 and OSC 9 escape sequences to the controlling TTY."""
    clean_title = title.replace(";", " ")
    clean_msg = message.replace(";", " ")
    osc777 = f"\033]777;notify;{clean_title};{clean_msg}\007"
    osc9 = f"\033]9;{clean_title}: {clean_msg}\007"

    is_tmux = bool(os.environ.get("TMUX"))
    if is_tmux:
        payload = f"\033Ptmux;\033{osc777}\033\\\033Ptmux;\033{osc9}\033\\"
    else:
        payload = f"{osc777}{osc9}"

    try:
        with open("/dev/tty", "w", encoding="utf-8") as tty:
            tty.write(payload)
            tty.flush()
    except OSError:
        pass
    else:
        return True

    try:
        if sys.stdout.isatty():
            sys.stdout.write(payload)
            sys.stdout.flush()
            return True
    except OSError as e:
        if verbose:
            sys.stderr.write(f"notify: terminal OSC emission failed: {e}\n")
    return False


def dispatch_notification(
    title: str,
    message: str,
    harness: str | None = None,
    icon: str | None = None,
    urgency: str = "normal",
    event_type: str = "completed",
    verbose: bool = False,
) -> None:
    """Route notification to terminal OSC and appropriate OS bridge with icon."""
    effective_title = title
    if harness and harness.lower() not in title.lower():
        effective_title = f"{harness} · {title}"

    icon_path = get_harness_icon(harness, custom_icon=icon)

    send_terminal_osc(effective_title, message, verbose=verbose)

    if is_wsl():
        send_wsl_toast(
            title=effective_title,
            message=message,
            harness=harness,
            icon_path=icon_path,
            verbose=verbose,
        )
    elif sys.platform == "darwin":
        send_macos_notification(effective_title, message, verbose=verbose)
    elif sys.platform.startswith("linux"):
        send_linux_notification(
            title=effective_title,
            message=message,
            icon_path=icon_path,
            urgency=urgency,
            verbose=verbose,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cross-platform agent notification dispatcher for WSL, macOS, and Linux."
    )
    cli_common.add_verbosity_args(parser)
    parser.add_argument(
        "positional_message",
        nargs="?",
        default=None,
        metavar="MESSAGE",
        help="notification body text",
    )
    parser.add_argument(
        "--title",
        "-t",
        default="Agent Notification",
        help="notification title (default: 'Agent Notification')",
    )
    parser.add_argument(
        "--message",
        "-m",
        default=None,
        help="notification body text (overrides positional message)",
    )
    parser.add_argument(
        "--harness",
        "-H",
        default=None,
        help="originating harness name (e.g. Claude, Pi, AGY, OpenCode, Copilot)",
    )
    parser.add_argument(
        "--icon",
        "-i",
        default=None,
        help="custom icon path or name",
    )
    parser.add_argument(
        "--urgency",
        "-u",
        choices=["low", "normal", "critical"],
        default="normal",
        help="urgency level (default: normal)",
    )
    parser.add_argument(
        "--type",
        choices=["completed", "waiting_for_input", "error"],
        default="completed",
        help="notification event type (default: completed)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    message = args.message or args.positional_message
    if not message:
        if args.type == "waiting_for_input":
            message = "Waiting for your input"
        elif args.type == "error":
            message = "Encountered an error"
        else:
            message = "Task completed"

    dispatch_notification(
        title=args.title,
        message=message,
        harness=args.harness,
        icon=args.icon,
        urgency=args.urgency,
        event_type=args.type,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
