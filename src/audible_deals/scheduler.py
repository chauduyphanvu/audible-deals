"""OS schedulers for unattended 'deals track run' (launchd/systemd/cron/schtasks)."""

from __future__ import annotations

import logging
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from audible_deals import paths
from audible_deals.logging_setup import NOTE
from audible_deals.storage import _atomic_write

logger = logging.getLogger(__name__)

LAUNCHD_LABEL = "com.audible-deals.track"
SYSTEMD_UNIT = "audible-deals-track"
SCHTASKS_NAME = "AudibleDealsTrack"
CRON_MARKER = "# audible-deals-track"
ENV_HOME = paths.ENV_HOME


class SchedulerError(Exception):
    """Raised when installing/removing the OS schedule fails."""


class NoSystemdBusError(SchedulerError):
    """systemd is installed but this session has no user manager to talk to.

    Typical in containers, WSL without systemd, and ssh sessions where the
    login did not start ``systemd --user``.
    """


_NO_BUS_SIGNS = ("failed to connect", "bus_address", "xdg_runtime_dir")


def _interval_text(interval_s: int) -> str:
    """*interval_s* the way ``--every`` is written (``6h``, ``1h30m``, ``45m``)."""
    hours, rest = divmod(interval_s, 3600)
    minutes, seconds = divmod(rest, 60)
    return (
        (f"{hours}h" if hours else "")
        + (f"{minutes}m" if minutes else "")
        + (f"{seconds}s" if seconds else "")
    ) or "0s"


def _foreground_hint(interval_s: int) -> str:
    return (
        f"To keep tracking without a system scheduler, run "
        f"'deals track run --every {_interval_text(interval_s)}' in a terminal, "
        "tmux or a service of your own."
    )


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


def generate_launchd_plist(
    cmd: list[str], interval_s: int, log_path: Path, data_dir: Path | None = None
) -> str:
    """The launchd agent definition, built with plistlib (so values are escaped)."""
    definition: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": list(cmd),
    }
    if data_dir is not None:
        definition["EnvironmentVariables"] = {ENV_HOME: str(data_dir)}
    definition.update(
        {
            "StartInterval": interval_s,
            "RunAtLoad": False,
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
        }
    )
    return plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=False).decode(
        "utf-8"
    )


def _write_unit(path: Path, content: str) -> None:
    """Write a unit/plist file atomically (a reader never sees half of it)."""
    _atomic_write(path, content)
    try:
        path.chmod(0o644)  # config, not a secret; launchd and systemd read it as you
    except OSError:
        pass


def _launchd_install(
    interval_s: int, log_path: Path, data_dir: Path | None = None
) -> str:
    plist = launchd_plist_path()
    _write_unit(
        plist, generate_launchd_plist(track_command(), interval_s, log_path, data_dir)
    )
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


def _systemd_environment(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"'


def generate_systemd_service(
    cmd: list[str], log_path: Path, data_dir: Path | None = None
) -> str:
    exec_start = " ".join(shlex.quote(c) for c in cmd)
    env = (
        f"{_systemd_environment(ENV_HOME, str(data_dir))}\n"
        if data_dir is not None
        else ""
    )
    return f"""[Unit]
Description=audible-deals background price tracking

[Service]
Type=oneshot
{env}ExecStart={exec_start}
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


#: What ``systemctl --user is-system-running`` prints while a user manager is
#: there to take a timer (a degraded one still runs timers). "offline" and
#: "unknown" mean there is none, and stderr then says the bus cannot be reached.
_USABLE_STATES = frozenset({"running", "degraded", "starting", "initializing"})


def _systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    proc = _run(["systemctl", "--user", "is-system-running"], check=False)
    return proc.returncode == 0 or proc.stdout.strip() in _USABLE_STATES


def _systemd_install(
    interval_s: int, log_path: Path, data_dir: Path | None = None
) -> str:
    unit_dir = systemd_unit_dir()
    units = {
        unit_dir / f"{SYSTEMD_UNIT}.service": generate_systemd_service(
            track_command(), log_path, data_dir
        ),
        unit_dir / f"{SYSTEMD_UNIT}.timer": generate_systemd_timer(interval_s),
    }
    before = {path: _read_bytes(path) for path in units}
    try:
        for path, content in units.items():
            _write_unit(path, content)
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"])
    except SchedulerError as exc:
        # An install that did not work leaves nothing behind: the files it wrote
        # go, and a schedule that was already there comes back as it was.
        for path, previous in before.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        if any(sign in str(exc).lower() for sign in _NO_BUS_SIGNS):
            raise NoSystemdBusError(str(exc)) from exc
        raise
    return f"systemd user timer {SYSTEMD_UNIT}.timer"


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


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


def generate_cron_line(
    cmd: list[str], interval_s: int, log_path: Path, data_dir: Path | None = None
) -> str:
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
    if data_dir is not None:
        exec_cmd = f"{ENV_HOME}={shlex.quote(str(data_dir))} {exec_cmd}"
    line = f"{exec_cmd} >> {shlex.quote(str(log_path))} 2>&1"
    # cron treats an unescaped % as a newline.
    return f"{schedule} {line.replace('%', chr(92) + '%')} {CRON_MARKER}"


def _read_crontab() -> list[str]:
    proc = _run(["crontab", "-l"], check=False)
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def _write_crontab(lines: list[str]) -> None:
    text = "\n".join(lines) + ("\n" if lines else "")
    proc = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SchedulerError(f"crontab update failed: {proc.stderr.strip()}")


def _cron_install(interval_s: int, log_path: Path, data_dir: Path | None = None) -> str:
    if not shutil.which("crontab"):
        raise SchedulerError(
            "Neither systemd user units nor crontab are available here. "
            + _foreground_hint(interval_s)
        )
    lines = [ln for ln in _read_crontab() if CRON_MARKER not in ln]
    lines.append(generate_cron_line(track_command(), interval_s, log_path, data_dir))
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


def generate_windows_task_command(
    cmd: list[str], log_path: Path, data_dir: Path | None = None
) -> str:
    if not cmd:
        raise SchedulerError("Windows scheduled task command cannot be empty")
    tokens = [str(token) for token in cmd]
    log = str(log_path)
    home = str(data_dir) if data_dir is not None else ""
    unsafe = {'"', "<", ">", "|", "\0", "\r", "\n", "%"}
    if any(char in value for value in [*tokens, log, home] for char in unsafe):
        raise SchedulerError(
            "Windows scheduled task command contains unsafe characters"
        )
    quoted = " ".join(_quote_windows_task_token(token) for token in tokens)
    prefix = f'set "{ENV_HOME}={home}" && ' if data_dir is not None else ""
    return f'cmd.exe /D /S /V:OFF /C "{prefix}{quoted} >> "{log}" 2>&1"'


def _schtasks_install(
    interval_s: int, log_path: Path, data_dir: Path | None = None
) -> str:
    cmd = generate_windows_task_command(track_command(), log_path, data_dir)
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
# What an installed schedule runs (for `deals doctor`)
# ---------------------------------------------------------------------------


def parse_launchd_command(plist_bytes: bytes) -> list[str]:
    """``ProgramArguments`` of a launchd agent definition."""
    try:
        data = plistlib.loads(plist_bytes)
    except Exception:  # plistlib raises several unrelated types on bad input
        return []
    arguments = data.get("ProgramArguments") if isinstance(data, dict) else None
    return [str(a) for a in arguments] if isinstance(arguments, list) else []


def parse_systemd_command(service_text: str) -> list[str]:
    """The ``ExecStart=`` command of a systemd service unit."""
    for line in service_text.splitlines():
        if line.startswith("ExecStart="):
            try:
                return shlex.split(line[len("ExecStart=") :])
            except ValueError:
                return []
    return []


def parse_cron_command(line: str) -> list[str]:
    """The command of one of our crontab lines (schedule and redirects removed)."""
    body = line.replace(CRON_MARKER, "")
    body = body.replace("\\%", "%")  # cron needs a literal % escaped
    fields = body.split(None, 5)
    if len(fields) < 6:
        return []
    try:
        tokens = shlex.split(fields[5])
    except ValueError:
        return []
    command: list[str] = []
    for token in tokens:
        if token in (">>", ">"):
            break
        if not command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue  # DEALS_HOME=... in front of the command
        command.append(token)
    return command


def parse_windows_task_command(task_to_run: str) -> list[str]:
    """The program and arguments inside our ``cmd.exe ... /C "..."`` task command."""
    text = task_to_run.strip()
    match = re.search(r'/C\s+"(.*)"\s*$', text)
    inner = match.group(1) if match else text
    if " && " in inner:  # the `set "DEALS_HOME=..."` prefix
        inner = inner.split(" && ", 1)[1]
    inner = inner.split(" >> ", 1)[0]  # the log redirect
    return re.findall(r'"([^"]*)"', inner)


def scheduled_command() -> list[str] | None:
    """The command the installed schedule runs, or ``None`` when it cannot be read."""
    try:
        if sys.platform == "darwin":
            plist = launchd_plist_path()
            return parse_launchd_command(plist.read_bytes()) or None
        if sys.platform.startswith("linux"):
            service = systemd_unit_dir() / f"{SYSTEMD_UNIT}.service"
            if service.exists():
                return (
                    parse_systemd_command(service.read_text(encoding="utf-8")) or None
                )
            if shutil.which("crontab"):
                for line in _read_crontab():
                    if CRON_MARKER in line:
                        return parse_cron_command(line) or None
            return None
        if sys.platform == "win32":
            proc = _run(
                ["schtasks", "/Query", "/TN", SCHTASKS_NAME, "/V", "/FO", "LIST"],
                check=False,
            )
            for line in proc.stdout.splitlines():
                if line.strip().lower().startswith("task to run:"):
                    return (
                        parse_windows_task_command(line.split(":", 1)[1].strip())
                        or None
                    )
    except (OSError, UnicodeDecodeError):
        return None
    return None


def missing_executable() -> str | None:
    """The program the installed schedule runs, when it no longer exists."""
    command = scheduled_command()
    if not command:
        return None
    program = command[0]
    found = (
        os.path.isfile(program) and os.access(program, os.X_OK)
        if os.path.isabs(program) or os.sep in program
        else shutil.which(program) is not None
    )
    return None if found else program


# ---------------------------------------------------------------------------
# Platform dispatch
# ---------------------------------------------------------------------------


def install(interval_s: int, log_path: Path, data_dir: Path | None = None) -> str:
    """Install the OS schedule for 'deals track run'. Returns a description.

    The scheduled job is pinned to *data_dir* (default: the directory this
    process uses) through ``DEALS_HOME``, because launchd, systemd and cron do
    not inherit the shell environment that chose it.
    """
    data_dir = paths.data_dir() if data_dir is None else data_dir
    if sys.platform == "darwin":
        return _launchd_install(interval_s, log_path, data_dir)
    if sys.platform.startswith("linux"):
        if _systemd_available():
            try:
                return _systemd_install(interval_s, log_path, data_dir)
            except NoSystemdBusError:
                return _cron_instead(
                    interval_s,
                    log_path,
                    data_dir,
                    "systemd could not be reached from this session (no user bus: "
                    "typical in a container, WSL or an ssh session)",
                )
        if shutil.which("systemctl"):
            return _cron_instead(
                interval_s,
                log_path,
                data_dir,
                "systemd has no user manager running in this session",
            )
        return _cron_install(interval_s, log_path, data_dir)
    if sys.platform == "win32":
        return _schtasks_install(interval_s, log_path, data_dir)
    raise SchedulerError(f"Unsupported platform: {sys.platform}")


def _cron_instead(
    interval_s: int, log_path: Path, data_dir: Path | None, why: str
) -> str:
    """Install the crontab entry when systemd cannot be used, and say so."""
    if not shutil.which("crontab"):
        raise SchedulerError(
            f"{why[0].upper()}{why[1:]}, and crontab is not installed, so nothing "
            f"was installed. {_foreground_hint(interval_s)}"
        )
    description = _cron_install(interval_s, log_path, data_dir)
    logger.log(NOTE, "%s, so a crontab entry was installed instead.", why)
    return description


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
