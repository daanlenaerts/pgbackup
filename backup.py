"""PostgreSQL backup utility for Docker."""

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

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


@dataclass
class BackupResult:
    uri: str
    dbname: str
    hostname: str
    success: bool
    path: Path | None = None
    error: str | None = None


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
            dump_env = {**os.environ, "PGCONNECT_TIMEOUT": "30"}

            if age_public_key:
                with tmp.open("wb") as f:
                    pg_dump = subprocess.Popen(
                        ["pg_dump", "-Fc", tunneled_uri],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=dump_env,
                    )
                    age_proc = subprocess.Popen(
                        ["age", "-r", age_public_key],
                        stdin=pg_dump.stdout,
                        stdout=f,
                        stderr=subprocess.PIPE,
                    )
                    pg_dump.stdout.close()
                    age_stderr = age_proc.communicate(timeout=pg_dump_timeout)[1]
                    pg_dump.wait()

                if pg_dump.returncode != 0:
                    stderr = pg_dump.stderr.read().decode(errors="replace").strip()
                    log.error("pg_dump failed for %s: %s", label, stderr)
                    tmp.unlink(missing_ok=True)
                    return BackupResult(uri, dbname, hostname, success=False, error=stderr)
                if age_proc.returncode != 0:
                    stderr = age_stderr.decode(errors="replace").strip()
                    log.error("age encryption failed for %s: %s", label, stderr)
                    tmp.unlink(missing_ok=True)
                    return BackupResult(uri, dbname, hostname, success=False, error=f"age: {stderr}")

                log.info("Encrypted with age")
            else:
                with tmp.open("wb") as f:
                    proc = subprocess.run(
                        ["pg_dump", "-Fc", tunneled_uri],
                        stdout=f,
                        stderr=subprocess.PIPE,
                        env=dump_env,
                        timeout=pg_dump_timeout,
                    )
                if proc.returncode != 0:
                    stderr = proc.stderr.decode(errors="replace").strip()
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
