"""Standalone HTTP server for ``/pool/`` with a cross-process shared rate cap.

Two (or more) identical, independent copies of this process serve the
``.deb`` files of the same APT repository export; clients may reach each of
them directly.  Publishing still happens exclusively through the main
``app.main`` service -- this process only ever reads the exported document
tree, so it can run from a read-only mount of the data volume.

Every response-body byte is paid for from the shared token bucket
(:mod:`app.ratelimit`) before it is written to the connection, therefore the
aggregate over all of these processes never exceeds ``APT_REPO_RATE_BPS``
(plus the 64 KiB burst), split fairly between source IPs.

Configuration (environment)
---------------------------
``APT_REPO_DATA_DIR``        repository data dir (export root), ``/data``
``APT_REPO_POOL_PORT``       listen port, ``8081``
``APT_REPO_RATE_BPS``        shared byte-rate cap (must match on all peers)
``APT_REPO_RATE_BURST``      bucket capacity / permitted burst, ``65536``
``APT_REPO_RATE_ACTIVE_TTL`` seconds an idle source IP keeps its share, ``1.0``
``APT_REPO_LIMITER_DIR``     shared limiter state dir, ``<data_dir>/limiter``
"""

from __future__ import annotations

import os
import stat
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .ratelimit import (
    DEFAULT_ACTIVE_TTL,
    DEFAULT_BURST_BYTES,
    DEFAULT_RATE_BPS,
    SharedRateLimiter,
)

#: Body bytes are committed to the connection in slices of this size; each
#: slice is paid for from the shared buckets right before it is written.
_CHUNK = 32 * 1024


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


class _PoolServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    pool_root: Path
    limiter: SharedRateLimiter


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "apt-pool/1"
    sys_version = ""

    server: _PoolServer  # type: ignore[assignment]

    def _resolve_pool(self) -> Path | None:
        raw = urlsplit(self.path).path
        if not raw.startswith("/pool/"):
            return None
        rel = unquote(raw[len("/pool/") :])
        if not rel or "\x00" in rel:
            return None
        parts = rel.split("/")
        if any(part in ("", ".", "..") for part in parts):
            return None
        try:
            resolved = self.server.pool_root.joinpath(*parts).resolve(strict=True)
            resolved.relative_to(self.server.pool_root)
        except (OSError, ValueError):
            return None
        return resolved

    def _send_health(self, head: bool) -> None:
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _serve(self, head: bool) -> None:
        if urlsplit(self.path).path == "/healthz":
            self._send_health(head)
            return
        resolved = self._resolve_pool()
        if resolved is None:
            self.send_error(404, "not found")
            return
        try:
            fh = resolved.open("rb")
        except OSError:
            self.send_error(404, "not found")
            return
        try:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                self.send_error(404, "not found")
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/vnd.debian.binary-package"
            )
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            if head:
                return
            client_ip = self.client_address[0]
            limiter = self.server.limiter
            sent = 0
            started = time.monotonic()
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                # Pay for the slice before committing it to the connection.
                limiter.acquire(client_ip, len(chunk))
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    break
                sent += len(chunk)
            self.log_message(
                "sent %d body bytes to %s in %.2fs",
                sent,
                client_ip,
                time.monotonic() - started,
            )
        finally:
            fh.close()

    def do_GET(self) -> None:
        self._serve(head=False)

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    data_dir = Path(os.environ.get("APT_REPO_DATA_DIR", "/data"))
    port = _env_int("APT_REPO_POOL_PORT", 8081)
    rate = _env_int("APT_REPO_RATE_BPS", DEFAULT_RATE_BPS)
    burst = _env_int("APT_REPO_RATE_BURST", DEFAULT_BURST_BYTES)
    ttl = float(os.environ.get("APT_REPO_RATE_ACTIVE_TTL") or DEFAULT_ACTIVE_TTL)
    limiter_dir = Path(
        os.environ.get("APT_REPO_LIMITER_DIR") or (data_dir / "limiter")
    )
    pool_root = (data_dir / "export" / "pool").resolve()

    limiter = SharedRateLimiter(
        limiter_dir, rate_bps=rate, burst_bytes=burst, active_ttl=ttl
    )
    server = _PoolServer(("0.0.0.0", port), _Handler)
    server.pool_root = pool_root
    server.limiter = limiter
    sys.stderr.write(
        "pool server: serving %s on 0.0.0.0:%d "
        "(shared cap %d B/s, burst %d B, state %s)\n"
        % (pool_root, port, rate, burst, limiter.state_path)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
