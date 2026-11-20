"""Unit tests for the standalone /pool/ download server."""

from __future__ import annotations

import http.client
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.pool_server import _Handler, _PoolServer  # noqa: E402
from app.ratelimit import SharedRateLimiter  # noqa: E402


@pytest.fixture()
def server(tmp_path: Path):
    pool = tmp_path / "data" / "export" / "pool"
    target = pool / "main" / "d" / "demo"
    target.mkdir(parents=True)
    payload = bytes(range(256)) * 1024  # 256 KiB
    (target / "demo_1.0.0_amd64.deb").write_bytes(payload)
    httpd = _PoolServer(("127.0.0.1", 0), _Handler)
    httpd.pool_root = pool.resolve()
    httpd.limiter = SharedRateLimiter(
        tmp_path / "limiter", rate_bps=1024 * 1024 * 1024
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, payload
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()


def _get(httpd: _PoolServer, path: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    conn.request("GET", path)
    return conn.getresponse()


def test_serves_pool_file(server) -> None:
    httpd, payload = server
    resp = _get(httpd, "/pool/main/d/demo/demo_1.0.0_amd64.deb")
    assert resp.status == 200
    assert resp.read() == payload


def test_healthz(server) -> None:
    httpd, _ = server
    resp = _get(httpd, "/healthz")
    assert resp.status == 200
    assert resp.read() == b"ok\n"


@pytest.mark.parametrize(
    "path",
    [
        "/dists/stable/Release",                       # not under /pool/
        "/pool/../dists/stable/Release",               # dot segments
        "/pool/%2e%2e/%2e%2e/dists/stable/Release",    # encoded dot segments
        "/pool/main/d/demo/../../../dists/Release",    # escaping the pool root
        "/pool/main/d/demo/missing.deb",               # absent file
    ],
)
def test_rejects_non_pool_and_traversal(server, path: str) -> None:
    httpd, _ = server
    resp = _get(httpd, path)
    assert resp.status == 404
    resp.read()


def test_throttles_to_configured_rate(tmp_path: Path) -> None:
    pool_root = tmp_path / "data" / "export" / "pool"
    demo = pool_root / "main" / "d" / "demo"
    demo.mkdir(parents=True)
    (demo / "big.deb").write_bytes(b"x" * (1024 * 1024))
    httpd = _PoolServer(("127.0.0.1", 0), _Handler)
    httpd.pool_root = pool_root.resolve()
    httpd.limiter = SharedRateLimiter(
        tmp_path / "limiter", rate_bps=4 * 1024 * 1024, burst_bytes=64 * 1024
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        start = time.monotonic()
        resp = _get(httpd, "/pool/main/d/demo/big.deb")
        body = resp.read()
        elapsed = time.monotonic() - start
        assert len(body) == 1024 * 1024
        assert elapsed > 0.2  # 1 MiB at 4 MiB/s cannot arrive instantly
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
