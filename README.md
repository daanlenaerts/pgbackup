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

## PostgreSQL Backup User

Create a read-only role dedicated to backups:

```sql
CREATE ROLE pgbackup WITH LOGIN PASSWORD 'a-strong-password';
GRANT CONNECT ON DATABASE mydb TO pgbackup;
-- connect to mydb, then:
GRANT USAGE ON SCHEMA public TO pgbackup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pgbackup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO pgbackup;
```

Then use it in the connection string:

```
PG_CONNECTIONS="postgresql://pgbackup:a-strong-password@host:5432/mydb"
```

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

### Setting Up a Limited SSH User

On the bastion/jump host (Ubuntu), create a dedicated user that can only forward TCP connections and nothing else:

```bash
# Create a user with no password and no home directory shell access
sudo adduser --disabled-password --gecos "pgbackup tunnel" pgbackup

# Generate a key pair on the machine running the container
ssh-keygen -t ed25519 -f pgbackup_key -N "" -C "pgbackup"

# Copy the public key to the bastion host
sudo mkdir -p /home/pgbackup/.ssh
sudo cp pgbackup_key.pub /home/pgbackup/.ssh/authorized_keys
sudo chown -R pgbackup:pgbackup /home/pgbackup/.ssh
sudo chmod 700 /home/pgbackup/.ssh
sudo chmod 600 /home/pgbackup/.ssh/authorized_keys
```

Lock the user down to port-forwarding only by setting its shell to `nologin` and restricting the authorized key:

```bash
# Disable interactive shell
sudo usermod -s /usr/sbin/nologin pgbackup

# Prefix the key in authorized_keys to allow only tunnel forwarding:
# no-pty,no-X11-forwarding,no-agent-forwarding,command="/bin/false" ssh-ed25519 AAAA... pgbackup
sudo sed -i 's|^ssh-|no-pty,no-X11-forwarding,no-agent-forwarding,command="/bin/false" ssh-|' \
  /home/pgbackup/.ssh/authorized_keys
```

Then pass the private key to the container:

```bash
docker run -d \
  -e PG_CONNECTIONS="postgresql://user:pass@db-host:5432/mydb" \
  -e SSH_HOST="pgbackup@bastion.example.com" \
  -e SSH_KEY="$(cat pgbackup_key)" \
  -v pgbackups:/backups \
  pgbackup
```
