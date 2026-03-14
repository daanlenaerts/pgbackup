"""SSH tunnel support for pgbackup."""

import io
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

log = logging.getLogger("pgbackup")


@dataclass
class SshConfig:
    host: str
    port: int
    username: str
    pkey: object | None  # paramiko key object


def parse_ssh_config() -> SshConfig | None:
    ssh_host = os.environ.get("SSH_HOST", "").strip()
    if not ssh_host:
        return None

    import paramiko

    # Parse user@host[:port]
    username = "root"
    if "@" in ssh_host:
        username, ssh_host = ssh_host.rsplit("@", 1)

    port = 22
    if ":" in ssh_host:
        ssh_host, port_str = ssh_host.rsplit(":", 1)
        port = int(port_str)

    # Parse optional SSH_KEY (PEM content)
    pkey = None
    ssh_key = os.environ.get("SSH_KEY", "").strip()
    if ssh_key:
        # Strip marker lines, convert spaces to newlines, re-add markers.
        import re
        begin = re.search(r"-----BEGIN [^-]+-----", ssh_key)
        end = re.search(r"-----END [^-]+-----", ssh_key)
        if not begin or not end:
            log.error("SSH_KEY missing BEGIN/END markers")
            sys.exit(1)
        body = ssh_key[begin.end():end.start()].strip().replace(" ", "\n")
        ssh_key = f"{begin.group()}\n{body}\n{end.group()}\n"

        key_classes = [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]
        for cls in key_classes:
            try:
                pkey = cls.from_private_key(io.StringIO(ssh_key))
                break
            except Exception:
                continue
        if pkey is None:
            log.error("SSH_KEY could not be parsed as Ed25519, RSA, or ECDSA")
            sys.exit(1)

    return SshConfig(host=ssh_host, port=port, username=username, pkey=pkey)


@contextmanager
def ssh_tunnel_for_uri(uri: str, ssh_config: SshConfig | None):
    if ssh_config is None:
        yield uri
        return

    from sshtunnel import SSHTunnelForwarder

    parsed = urlparse(uri)
    remote_host = parsed.hostname or "localhost"
    remote_port = parsed.port or 5432

    tunnel_kwargs = {
        "ssh_address_or_host": (ssh_config.host, ssh_config.port),
        "ssh_username": ssh_config.username,
        "remote_bind_address": (remote_host, remote_port),
    }
    if ssh_config.pkey:
        tunnel_kwargs["ssh_pkey"] = ssh_config.pkey

    tunnel = SSHTunnelForwarder(**tunnel_kwargs)
    tunnel.start()
    try:
        local_port = tunnel.local_bind_port
        log.info(
            "SSH tunnel open: localhost:%d → %s:%d via %s@%s:%d",
            local_port, remote_host, remote_port,
            ssh_config.username, ssh_config.host, ssh_config.port,
        )
        # Rewrite URI to go through the tunnel
        tunneled = parsed._replace(
            netloc=f"{parsed.username}:{parsed.password}@localhost:{local_port}"
            if parsed.password
            else f"{parsed.username}@localhost:{local_port}"
            if parsed.username
            else f"localhost:{local_port}",
        )
        yield urlunparse(tunneled)
    finally:
        tunnel.stop()
        log.info("SSH tunnel closed")
