"""OS schedulers for unattended 'deals track run' (launchd/systemd/cron/schtasks)."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

LAUNCHD_LABEL = "com.audible-deals.track"
SYSTEMD_UNIT = "audible-deals-track"
SCHTASKS_NAME = "AudibleDealsTrack"
CRON_MARKER = "# audible-deals-track"


class SchedulerError(Exception):
    """Raised when installing/removing the OS schedule fails."""


def track_command() -> list[str]:
    """Absolute command to run 'deals track run', resolved at install time."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "track", "run"]
    exe = shutil.which("deals")
    if exe:
        return [exe, "track", "run"]
    return [sys.executable, "-m", "audible_deals", "track", "run"]


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("scheduler exec: %s", cmd)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SchedulerError(
            f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


# ---------------------------------------------------------------------------
# macOS (launchd)
# ---------------------------------------------------------------------------


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def generate_launchd_plist(cmd: list[str], interval_s: int, log_path: Path) -> str:
    args = "\n".join(f"        <string>{escape(c)}</string>" for c in cmd)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>StartInterval</key>
    <integer>{interval_s}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log_path))}</string>
</dict>
</plist>
"""


def _launchd_install(interval_s: int, log_path: Path) -> str:
    plist = launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(generate_launchd_plist(track_command(), interval_s, log_path))
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", domain, str(plist)], check=False)
    proc = _run(["launchctl", "bootstrap", domain, str(plist)], check=False)
    if proc.returncode != 0:
        # Older macOS releases lack bootstrap; fall back to load
        _run(["launchctl", "load", str(plist)])
    return f"launchd agent {plist}"


def _launchd_uninstall() -> bool:
    plist = launchd_plist_path()
    if not plist.exists():
        return False
    _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False)
    plist.unlink()
    return True


# ---------------------------------------------------------------------------
# Linux (systemd user units, cron fallback)
# ---------------------------------------------------------------------------


def systemd_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def generate_systemd_service(cmd: list[str], log_path: Path) -> str:
    exec_start = " ".join(shlex.quote(c) for c in cmd)
    return f"""[Unit]
Description=audible-deals background price tracking

[Service]
Type=oneshot
ExecStart={exec_start}
StandardOutput=append:{log_path}
StandardError=append:{log_path}
"""


def generate_systemd_timer(interval_s: int) -> str:
    return f"""[Unit]
Description=audible-deals background price tracking timer

[Timer]
OnBootSec=5min
OnUnitActiveSec={interval_s}s
Persistent=true

[Install]
WantedBy=timers.target
"""


def _systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    proc = _run(["systemctl", "--user", "is-system-running"], check=False)
    # Degraded systemd still schedules timers; only a missing user bus disqualifies
    return proc.returncode == 0 or bool(proc.stdout.strip())


def _systemd_install(interval_s: int, log_path: Path) -> str:
    unit_dir = systemd_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{SYSTEMD_UNIT}.service").write_text(
        generate_systemd_service(track_command(), log_path)
    )
    (unit_dir / f"{SYSTEMD_UNIT}.timer").write_text(generate_systemd_timer(interval_s))
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"])
    return f"systemd user timer {SYSTEMD_UNIT}.timer"


def _systemd_uninstall() -> bool:
    unit_dir = systemd_unit_dir()
    service = unit_dir / f"{SYSTEMD_UNIT}.service"
    timer = unit_dir / f"{SYSTEMD_UNIT}.timer"
    if not service.exists() and not timer.exists():
        return False
    _run(
        ["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"],
        check=False,
    )
    service.unlink(missing_ok=True)
    timer.unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    return True


def generate_cron_line(cmd: list[str], interval_s: int, log_path: Path) -> str:
    if interval_s % 60:
        raise SchedulerError(
            "cron cannot represent intervals with seconds; use a whole number of minutes"
        )
    minutes = interval_s // 60
    if minutes < 1:
        raise SchedulerError("cron interval must be at least one minute")
    if minutes < 60:
        if 60 % minutes:
            raise SchedulerError(
                f"cron cannot represent an exact {minutes}-minute interval"
            )
        schedule = f"*/{minutes} * * * *"
    elif minutes == 60:
        schedule = "0 * * * *"
    elif minutes == 24 * 60:
        schedule = "0 0 * * *"
    else:
        if minutes % 60:
            raise SchedulerError(
                f"cron cannot represent an exact {minutes}-minute interval"
            )
        hours = minutes // 60
        if hours > 24 or 24 % hours:
            raise SchedulerError(
                f"cron cannot represent an exact {hours}-hour interval"
            )
        schedule = f"0 */{hours} * * *"
    exec_cmd = " ".join(shlex.quote(c) for c in cmd)
    return f"{schedule} {exec_cmd} >> {shlex.quote(str(log_path))} 2>&1 {CRON_MARKER}"


def _read_crontab() -> list[str]:
    proc = _run(["crontab", "-l"], check=False)
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def _write_crontab(lines: list[str]) -> None:
    text = "\n".join(lines) + ("\n" if lines else "")
    proc = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SchedulerError(f"crontab update failed: {proc.stderr.strip()}")


def _cron_install(interval_s: int, log_path: Path) -> str:
    if not shutil.which("crontab"):
        raise SchedulerError("Neither systemd user units nor crontab are available")
    lines = [ln for ln in _read_crontab() if CRON_MARKER not in ln]
    lines.append(generate_cron_line(track_command(), interval_s, log_path))
    _write_crontab(lines)
    return "crontab entry"


def _cron_uninstall() -> bool:
    if not shutil.which("crontab"):
        return False
    lines = _read_crontab()
    kept = [ln for ln in lines if CRON_MARKER not in ln]
    if len(kept) == len(lines):
        return False
    _write_crontab(kept)
    return True


# ---------------------------------------------------------------------------
# Windows (schtasks)
# ---------------------------------------------------------------------------


def _quote_windows_task_token(value: str) -> str:
    trailing_backslashes = len(value) - len(value.rstrip("\\"))
    if trailing_backslashes:
        value += "\\" * trailing_backslashes
    return f'"{value}"'


def generate_windows_task_command(cmd: list[str], log_path: Path) -> str:
    if not cmd:
        raise SchedulerError("Windows scheduled task command cannot be empty")
    tokens = [str(token) for token in cmd]
    log = str(log_path)
    unsafe = {'"', "<", ">", "|", "\0", "\r", "\n", "%"}
    if any(char in value for value in [*tokens, log] for char in unsafe):
        raise SchedulerError(
            "Windows scheduled task command contains unsafe characters"
        )
    quoted = " ".join(_quote_windows_task_token(token) for token in tokens)
    return f'cmd.exe /D /S /V:OFF /C "{quoted} >> "{log}" 2>&1"'


def _schtasks_install(interval_s: int, log_path: Path) -> str:
    cmd = generate_windows_task_command(track_command(), log_path)
    if interval_s % 60:
        raise SchedulerError(
            "Windows Task Scheduler cannot represent intervals with seconds; use a whole number of minutes"
        )
    minutes = interval_s // 60
    if minutes < 1:
        raise SchedulerError(
            "Windows Task Scheduler interval must be at least one minute"
        )
    if minutes < 60 * 24:
        schedule = ["/SC", "MINUTE", "/MO", str(minutes)]
    else:
        if minutes % (60 * 24):
            raise SchedulerError(
                f"Windows Task Scheduler cannot represent an exact {minutes}-minute interval"
            )
        days = minutes // (60 * 24)
        if days > 365:
            raise SchedulerError(
                "Windows Task Scheduler supports intervals up to 365 days"
            )
        schedule = ["/SC", "DAILY", "/MO", str(days)]
    _run(["schtasks", "/Create", "/F", "/TN", SCHTASKS_NAME, "/TR", cmd, *schedule])
    return f"scheduled task {SCHTASKS_NAME}"


def _schtasks_uninstall() -> bool:
    proc = _run(["schtasks", "/Delete", "/F", "/TN", SCHTASKS_NAME], check=False)
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Platform dispatch
# ---------------------------------------------------------------------------


def install(interval_s: int, log_path: Path) -> str:
    """Install the OS schedule for 'deals track run'. Returns a description."""
    if sys.platform == "darwin":
        return _launchd_install(interval_s, log_path)
    if sys.platform.startswith("linux"):
        if _systemd_available():
            return _systemd_install(interval_s, log_path)
        return _cron_install(interval_s, log_path)
    if sys.platform == "win32":
        return _schtasks_install(interval_s, log_path)
    raise SchedulerError(f"Unsupported platform: {sys.platform}")


def uninstall() -> bool:
    """Remove the OS schedule. Returns True when something was removed."""
    if sys.platform == "darwin":
        return _launchd_uninstall()
    if sys.platform.startswith("linux"):
        # Remove unit files whenever they exist, mirroring installed()'s
        # file-existence check; _systemd_uninstall is safe when the user bus
        # is unavailable (it runs systemctl with check=False).
        removed = _systemd_uninstall()
        return _cron_uninstall() or removed
    if sys.platform == "win32":
        return _schtasks_uninstall()
    raise SchedulerError(f"Unsupported platform: {sys.platform}")


def installed() -> tuple[bool, str]:
    """Whether the OS schedule exists, and where."""
    if sys.platform == "darwin":
        plist = launchd_plist_path()
        return plist.exists(), str(plist)
    if sys.platform.startswith("linux"):
        timer = systemd_unit_dir() / f"{SYSTEMD_UNIT}.timer"
        if timer.exists():
            return True, str(timer)
        if shutil.which("crontab") and any(CRON_MARKER in ln for ln in _read_crontab()):
            return True, "crontab entry"
        return False, str(timer)
    if sys.platform == "win32":
        proc = _run(["schtasks", "/Query", "/TN", SCHTASKS_NAME], check=False)
        return proc.returncode == 0, SCHTASKS_NAME
    return False, f"unsupported platform {sys.platform}"
