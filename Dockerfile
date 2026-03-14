FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client && \
    rm -rf /var/lib/apt/lists/*

COPY backup.py ssh.py telegram.py pyproject.toml /app/
WORKDIR /app

# UV sync
RUN uv sync --no-dev --frozen

ENV BACKUP_DIR=/backups
VOLUME /backups

CMD ["uv", "run", "--script", "backup.py"]
