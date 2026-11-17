"""Who may talk to the server (plans.md §6.3).

The server can start agents that run tools, so a web page from any other site must not be able
to reach it. Three checks, all of which must pass:

1. `Host` names this machine by a loopback name. That stops DNS rebinding, where a hostile site
   points its own name at 127.0.0.1.
2. `Origin`, when a browser sends one, is this server (or the UI's dev server).
3. Every `/api` request and the WebSocket carry the secret token from the URL `apm serve` prints.
"""

import hmac
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from apm.core.lock import CACHE_DIR

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SERVER_FILE = "server.json"


def host_is_loopback(host_header: str | None) -> bool:
    """True for `127.0.0.1`, `localhost` or `[::1]`, with or without a port."""
    if not host_header:
        return False
    if host_header.startswith("["):
        host = host_header.partition("]")[0] + "]"
    else:
        host = host_header.partition(":")[0]
    return host.lower() in LOOPBACK_HOSTS


def origin_allowed(origin: str | None, host_header: str | None, extra: frozenset[str]) -> bool:
    """No `Origin` at all is fine: that's curl or the CLI, which still need the token."""
    if origin is None:
        return True
    if origin in extra:
        return True
    parts = urlsplit(origin)
    return parts.scheme == "http" and parts.netloc == host_header and host_is_loopback(parts.netloc)


def token_matches(given: str | None, token: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), token.encode())


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def load_or_create_token(apm_dir: Path) -> str:
    """The token is kept between runs, so a bookmarked URL keeps working."""
    path = apm_dir / CACHE_DIR / SERVER_FILE
    try:
        token = json.loads(path.read_text(encoding="utf-8")).get("token")
        if isinstance(token, str) and len(token) >= 32:
            return token
    except OSError, ValueError, AttributeError:
        pass
    return secrets.token_urlsafe(32)


def write_server_file(apm_dir: Path, *, token: str, port: int) -> None:
    """Records the running server, readable only by you."""
    path = apm_dir / CACHE_DIR / SERVER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"token": token, "port": port, "pid": os.getpid()}, f)
    path.chmod(0o600)
