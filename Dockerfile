FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client && \
    rm -rf /var/lib/apt/lists/*

COPY backup.py telegram.py /app/
WORKDIR /app

# Pre-install script deps
RUN uv run --script backup.py --help 2>/dev/null || true

ENV BACKUP_DIR=/backups
VOLUME /backups

CMD ["uv", "run", "--script", "backup.py"]
