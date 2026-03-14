# pgbackup

Very simple PostgreSQL backup utility that runs as a Docker container. Runs `pg_dump` on a cron schedule, manages retention, and optionally alerts on failure via webhook or Telegram.

## Quick Start

```bash
docker build -t pgbackup .
docker run -d \
  -e PG_CONNECTIONS="postgresql://user:pass@host:5432/db" \
  -e RUN_ON_STARTUP=true \
  -v pgbackups:/backups \
  pgbackup
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `PG_CONNECTIONS` | Yes | — | Comma-separated postgres connection URIs |
| `BACKUP_CRON` | No | `0 2 * * *` | Cron schedule (default: daily 2am) |
| `BACKUP_DIR` | No | `/backups` | Backup storage path |
| `RETENTION_DAYS` | No | `7` | Days to keep backups |
| `WEBHOOK_URL` | No | — | URL to POST failure notifications (Slack/Discord/ntfy) |
| `TELEGRAM_BOT_TOKEN` | No | — | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_IDS` | No | — | Comma-separated Telegram chat IDs to notify |
| `RUN_ON_STARTUP` | No | `false` | Run backup immediately before entering cron loop |
| `SSH_HOST` | No | — | SSH tunnel jump host in `user@host[:port]` format |
| `SSH_KEY` | No | — | PEM private key content (falls back to SSH agent if unset) |

## Backups

Backups are stored as `pg_dump` custom format (`.dump`) files named `{dbname}_{timestamp}.dump`. Restore with:

```bash
pg_restore -d <target_db> <file>.dump
```

## Notifications

**Webhook**: Posts `{"text": "..."}` on failure — compatible with Slack, Discord, and ntfy.

**Telegram**: Sends Markdown-formatted messages to one or more chats when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` are set.

## SSH Tunnels

To back up a database behind an SSH bastion/jump host, set `SSH_HOST`:

```bash
docker run -d \
  -e PG_CONNECTIONS="postgresql://user:pass@db-host:5432/mydb" \
  -e SSH_HOST="ubuntu@bastion.example.com" \
  -e SSH_KEY="$(cat ~/.ssh/id_ed25519)" \
  -v pgbackups:/backups \
  pgbackup
```

The tunnel forwards a random local port to the database host:port extracted from each connection URI. Without `SSH_HOST`, behavior is unchanged. If `SSH_KEY` is omitted, the SSH agent or default keys are used.
