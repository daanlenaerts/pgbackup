"""PostgreSQL backup utility for Docker."""

import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from croniter import croniter

import httpx

import ssh
import telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pgbackup")

shutdown_requested = False


def _handle_signal(signum: int, _frame: object) -> None:
    global shutdown_requested
    log.info("Received %s, shutting down gracefully…", signal.Signals(signum).name)
    shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


@dataclass
class Config:
    connections: list[str]
    backup_dir: Path
    retention_days: int
    cron_expr: str
    webhook_url: str | None
    telegram_token: str | None
    telegram_chat_ids: list[str]
    run_on_startup: bool
    ssh: ssh.SshConfig | None
    pg_dump_timeout: int
    age_public_key: str | None
    timestamp_fmt: str = field(default="%Y%m%d_%H%M%S", init=False)


def parse_config() -> Config:
    raw = os.environ.get("PG_CONNECTIONS", "").strip()
    if not raw:
        log.error("PG_CONNECTIONS is required")
        sys.exit(1)
    connections = [c.strip() for c in raw.split(",") if c.strip()]
    if not connections:
        log.error("PG_CONNECTIONS contains no valid URIs")
        sys.exit(1)

    return Config(
        connections=connections,
        backup_dir=Path(os.environ.get("BACKUP_DIR", "/backups")),
        retention_days=int(os.environ.get("RETENTION_DAYS", "7")),
        cron_expr=os.environ.get("BACKUP_CRON", "0 2 * * *"),
        webhook_url=os.environ.get("WEBHOOK_URL") or None,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_ids=[
            c.strip()
            for c in (os.environ.get("TELEGRAM_CHAT_IDS") or "").split(",")
            if c.strip()
        ],
        run_on_startup=os.environ.get("RUN_ON_STARTUP", "false").lower() in ("true", "1", "yes"),
        ssh=ssh.parse_ssh_config(),
        pg_dump_timeout=int(os.environ.get("PG_DUMP_TIMEOUT", "3600")),
        age_public_key=os.environ.get("AGE_PUBLIC_KEY") or None,
    )


def extract_db_info(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    dbname = parsed.path.lstrip("/") or "unknown"
    hostname = parsed.hostname or "unknown"
    return dbname, hostname


def _add_keepalive_params(uri: str) -> str:
    """Add TCP keepalive parameters to a PostgreSQL connection URI."""
    parsed = urlparse(uri)
    params = parse_qs(parsed.query)
    params.setdefault("keepalives", ["1"])
    params.setdefault("keepalives_idle", ["60"])
    params.setdefault("keepalives_interval", ["10"])
    params.setdefault("keepalives_count", ["5"])
    params.setdefault("tcp_user_timeout", ["60000"])
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


STALL_TIMEOUT = 300  # seconds with no output growth before declaring stall
POLL_INTERVAL = 10
PROGRESS_INTERVAL = 60


@dataclass
class BackupResult:
    uri: str
    dbname: str
    hostname: str
    success: bool
    path: Path | None = None
    error: str | None = None


def _kill_procs(*procs: subprocess.Popen) -> None:
    """Kill and reap subprocesses."""
    for p in procs:
        if p.poll() is None:
            p.kill()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


@dataclass
class _StallInfo:
    reason: str  # "stall" or "timeout"
    last_size: int


def _monitor_procs(
    target: subprocess.Popen,
    tmp: Path,
    label: str,
    timeout: int,
) -> _StallInfo | None:
    """Poll until target process exits. Returns stall info or None on success."""
    last_size = 0
    last_growth = time.monotonic()
    last_log = 0.0
    start = time.monotonic()

    while target.poll() is None:
        time.sleep(POLL_INTERVAL)
        elapsed = time.monotonic() - start

        if elapsed > timeout:
            return _StallInfo("timeout", last_size)

        try:
            size = tmp.stat().st_size
        except FileNotFoundError:
            size = 0

        now = time.monotonic()
        if size > last_size:
            last_size = size
            last_growth = now
        elif now - last_growth > STALL_TIMEOUT and last_size > 0:
            return _StallInfo("stall", last_size)

        if now - last_log >= PROGRESS_INTERVAL:
            rate = last_size / elapsed if elapsed > 0 else 0
            log.info(
                "%s: %.1f GB written, %.1f MB/s (%dm%02ds elapsed)",
                label,
                last_size / (1024 ** 3),
                rate / (1024 ** 2),
                int(elapsed) // 60,
                int(elapsed) % 60,
            )
            last_log = now

    return None


def _proc_status(proc: subprocess.Popen) -> str:
    """Describe a process's current state."""
    rc = proc.poll()
    if rc is None:
        return "still running"
    if rc < 0:
        try:
            sig_name = signal.Signals(-rc).name
        except (ValueError, KeyError):
            sig_name = str(-rc)
        return f"killed by {sig_name}"
    if rc == 0:
        return "exited normally"
    return f"exited with code {rc}"


def _read_stderr(source: subprocess.Popen | tempfile.SpooledTemporaryFile | object) -> str:
    """Read stderr from a Popen pipe or a file object used as stderr."""
    if isinstance(source, subprocess.Popen):
        pipe = source.stderr
        if pipe is None:
            return ""
        try:
            return pipe.read().decode(errors="replace").strip()
        except Exception:
            return ""
    # file-like (e.g. TemporaryFile used as pg_dump stderr)
    try:
        source.seek(0)
        return source.read().decode(errors="replace").strip()
    except Exception:
        return ""


def backup_database(uri: str, backup_dir: Path, timestamp: str, ssh_config: ssh.SshConfig | None = None, pg_dump_timeout: int = 3600, age_public_key: str | None = None) -> BackupResult:
    dbname, hostname = extract_db_info(uri)
    label = f"{dbname}@{hostname}"
    suffix = ".dump.age" if age_public_key else ".dump"
    filename = f"{dbname}_{timestamp}{suffix}"
    dest = backup_dir / filename
    tmp = dest.with_name(dest.name + ".tmp")

    log.info("Backing up %s", label)
    try:
        with ssh.ssh_tunnel_for_uri(uri, ssh_config) as tunneled_uri:
            dump_uri = _add_keepalive_params(tunneled_uri)
            dump_env = {
                **os.environ,
                "PGCONNECT_TIMEOUT": "30",
                "PGOPTIONS": "-c statement_timeout=0 -c idle_in_transaction_session_timeout=0",
            }

            if age_public_key:
                with tmp.open("wb") as f, tempfile.TemporaryFile() as pg_stderr_file:
                    pg_dump = subprocess.Popen(
                        ["pg_dump", "-Fc", dump_uri],
                        stdout=subprocess.PIPE,
                        stderr=pg_stderr_file,
                        env=dump_env,
                    )
                    age_proc = subprocess.Popen(
                        ["age", "-r", age_public_key],
                        stdin=pg_dump.stdout,
                        stdout=f,
                        stderr=subprocess.PIPE,
                    )
                    pg_dump.stdout.close()

                    stall = _monitor_procs(age_proc, tmp, label, pg_dump_timeout)
                    if stall:
                        pg_dump_state = _proc_status(pg_dump)
                        age_state = _proc_status(age_proc)
                        pg_stderr = _read_stderr(pg_stderr_file)
                        age_stderr = _read_stderr(age_proc)
                        _kill_procs(pg_dump, age_proc)
                        tmp.unlink(missing_ok=True)

                        size_str = f"{stall.last_size / (1024 ** 3):.1f} GB"
                        if stall.reason == "timeout":
                            msg = f"timed out after {pg_dump_timeout}s at {size_str}"
                        else:
                            msg = f"stalled at {size_str} — no data written for {STALL_TIMEOUT}s"
                        msg += f" | pg_dump: {pg_dump_state}"
                        if pg_stderr:
                            msg += f" ({pg_stderr})"
                        msg += f" | age: {age_state}"
                        if age_stderr:
                            msg += f" ({age_stderr})"

                        log.error("%s: %s", label, msg)
                        return BackupResult(uri, dbname, hostname, success=False, error=msg)

                    pg_dump.wait(timeout=60)

                    pg_stderr = _read_stderr(pg_stderr_file)
                    age_stderr = _read_stderr(age_proc)

                if pg_dump.returncode != 0:
                    log.error("pg_dump failed for %s: %s", label, pg_stderr)
                    tmp.unlink(missing_ok=True)
                    return BackupResult(uri, dbname, hostname, success=False, error=pg_stderr)
                if age_proc.returncode != 0:
                    log.error("age encryption failed for %s: %s", label, age_stderr)
                    tmp.unlink(missing_ok=True)
                    return BackupResult(uri, dbname, hostname, success=False, error=f"age: {age_stderr}")

                log.info("Encrypted with age")
            else:
                with tmp.open("wb") as f:
                    proc = subprocess.Popen(
                        ["pg_dump", "-Fc", dump_uri],
                        stdout=f,
                        stderr=subprocess.PIPE,
                        env=dump_env,
                    )

                    stall = _monitor_procs(proc, tmp, label, pg_dump_timeout)
                    if stall:
                        pg_dump_state = _proc_status(proc)
                        stderr = _read_stderr(proc)
                        _kill_procs(proc)
                        tmp.unlink(missing_ok=True)

                        size_str = f"{stall.last_size / (1024 ** 3):.1f} GB"
                        if stall.reason == "timeout":
                            msg = f"timed out after {pg_dump_timeout}s at {size_str}"
                        else:
                            msg = f"stalled at {size_str} — no data written for {STALL_TIMEOUT}s"
                        msg += f" | pg_dump: {pg_dump_state}"
                        if stderr:
                            msg += f" ({stderr})"

                        log.error("%s: %s", label, msg)
                        return BackupResult(uri, dbname, hostname, success=False, error=msg)

                stderr = _read_stderr(proc)
                if proc.returncode != 0:
                    log.error("pg_dump failed for %s: %s", label, stderr)
                    tmp.unlink(missing_ok=True)
                    return BackupResult(uri, dbname, hostname, success=False, error=stderr)

        tmp.rename(dest)
        size_mb = dest.stat().st_size / (1024 * 1024)
        log.info("Backed up %s → %s (%.1f MB)", label, dest.name, size_mb)
        return BackupResult(uri, dbname, hostname, success=True, path=dest)

    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        msg = f"pg_dump timed out after {pg_dump_timeout}s"
        log.error("%s: %s", label, msg)
        return BackupResult(uri, dbname, hostname, success=False, error=msg)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("%s: %s", label, exc)
        return BackupResult(uri, dbname, hostname, success=False, error=str(exc))


def cleanup_old_backups(backup_dir: Path, retention_days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for f in sorted(set(backup_dir.glob("*.dump")) | set(backup_dir.glob("*.dump.age"))):
        if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < cutoff:
            f.unlink()
            log.info("Removed expired backup: %s", f.name)
            removed += 1
    if removed:
        log.info("Cleaned up %d expired backup(s)", removed)


def notify_webhook(url: str, text: str) -> None:
    try:
        resp = httpx.post(url, json={"text": text}, timeout=15)
        resp.raise_for_status()
        log.info("Webhook notification sent")
    except Exception as exc:
        log.warning("Webhook notification failed: %s", exc)



def run_backup_cycle(config: Config) -> None:
    timestamp = datetime.now().strftime(config.timestamp_fmt)
    config.backup_dir.mkdir(parents=True, exist_ok=True)

    results = [backup_database(uri, config.backup_dir, timestamp, config.ssh, config.pg_dump_timeout, config.age_public_key) for uri in config.connections]

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    log.info("Cycle complete: %d succeeded, %d failed", len(successes), len(failures))

    cleanup_old_backups(config.backup_dir, config.retention_days)

    if failures:
        lines = [f"⚠️ pgbackup: {len(failures)} backup(s) failed:"]
        for f in failures:
            lines.append(f"• {f.dbname}@{f.hostname}: {f.error}")
        failure_msg = "\n".join(lines)
        if config.webhook_url:
            notify_webhook(config.webhook_url, failure_msg)
        if config.telegram_token and config.telegram_chat_ids:
            telegram.send(config.telegram_token, config.telegram_chat_ids, failure_msg)


def main() -> None:
    config = parse_config()
    log.info(
        "pgbackup started — %d database(s), schedule=%s, retention=%dd, encryption=%s",
        len(config.connections),
        config.cron_expr,
        config.retention_days,
        "enabled" if config.age_public_key else "disabled",
    )

    startup_msg = (
        f"pgbackup started — {len(config.connections)} database(s), "
        f"schedule=`{config.cron_expr}`, retention={config.retention_days}d, "
        f"encryption={'enabled' if config.age_public_key else 'disabled'}"
    )
    if config.webhook_url:
        notify_webhook(config.webhook_url, startup_msg)
    if config.telegram_token and config.telegram_chat_ids:
        telegram.send(config.telegram_token, config.telegram_chat_ids, startup_msg)

    if config.run_on_startup:
        log.info("Running startup backup cycle")
        run_backup_cycle(config)

    cron = croniter(config.cron_expr)
    while not shutdown_requested:
        next_run = cron.get_next(float)
        next_dt = datetime.fromtimestamp(next_run)
        log.info("Next backup at %s", next_dt.strftime("%Y-%m-%d %H:%M:%S"))

        while not shutdown_requested and time.time() < next_run:
            time.sleep(min(30, max(0, next_run - time.time())))

        if not shutdown_requested:
            run_backup_cycle(config)

    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
