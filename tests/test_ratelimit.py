"""Unit tests for the cross-process shared rate limiter."""

from __future__ import annotations

import json
import multiprocessing
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.ratelimit import SharedRateLimiter  # noqa: E402

MIB = 1024 * 1024


def test_single_source_gets_full_rate(tmp_path: Path) -> None:
    limiter = SharedRateLimiter(tmp_path, rate_bps=4 * MIB, burst_bytes=64 * 1024)
    start = time.monotonic()
    for _ in range(32):  # 32 * 32 KiB = 1 MiB
        limiter.acquire("10.0.0.1", 32 * 1024)
    elapsed = time.monotonic() - start
    # 1 MiB at 4 MiB/s with a 64 KiB initial burst: >= ~0.23 s, well under 1 s.
    assert 0.2 < elapsed < 1.0


def test_two_ips_split_evenly(tmp_path: Path) -> None:
    rate = 4 * MIB
    limiter = SharedRateLimiter(tmp_path, rate_bps=rate, burst_bytes=64 * 1024)
    granted = {"10.0.0.1": 0, "10.0.0.2": 0}
    start = time.monotonic()
    while time.monotonic() - start < 1.0:
        for ip in granted:
            before = time.monotonic()
            limiter.acquire(ip, 32 * 1024)
            # keep both IPs continuously backlogged
            if time.monotonic() - before > 0.5:
                pytest.fail("grant stalled")
            granted[ip] += 32 * 1024
    ratio = granted["10.0.0.1"] / granted["10.0.0.2"]
    assert 0.7 < ratio < 1.4, granted
    total = sum(granted.values())
    assert total <= rate * 1.05 + 64 * 1024


def _child_grants(state_dir: str, ip: str, amount: int, queue) -> None:
    limiter = SharedRateLimiter(state_dir, rate_bps=2 * MIB, burst_bytes=64 * 1024)
    for _ in range(amount // (32 * 1024)):
        limiter.acquire(ip, 32 * 1024)
    queue.put(amount)


def test_cap_holds_across_processes(tmp_path: Path) -> None:
    rate = 2 * MIB
    queue = multiprocessing.Queue()
    start = time.monotonic()
    procs = [
        multiprocessing.Process(
            target=_child_grants, args=(str(tmp_path), f"10.1.1.{i}", MIB, queue)
        )
        for i in range(2)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
    assert all(proc.exitcode == 0 for proc in procs)
    elapsed = time.monotonic() - start
    # Two processes each pull 1 MiB; the shared cap is 2 MiB/s with a 64 KiB
    # burst, so this must take at least ~(2 MiB - 64 KiB) / 2 MiB/s.
    assert elapsed > (2 * MIB - 64 * 1024) / rate * 0.9


def test_state_survives_reopen(tmp_path: Path) -> None:
    first = SharedRateLimiter(tmp_path, rate_bps=MIB, burst_bytes=64 * 1024)
    first.acquire("10.0.0.9", 64 * 1024)  # drain the initial burst
    second = SharedRateLimiter(tmp_path, rate_bps=MIB, burst_bytes=64 * 1024)
    start = time.monotonic()
    second.acquire("10.0.0.9", 64 * 1024)  # must wait for a refill, not a new bucket
    assert time.monotonic() - start > 0.03


def test_corrupt_state_self_heals(tmp_path: Path) -> None:
    limiter = SharedRateLimiter(tmp_path, rate_bps=16 * MIB, burst_bytes=64 * 1024)
    limiter.acquire("10.0.0.1", 1024)
    limiter.state_path.write_bytes(b'{"global": {"tokens":')  # torn write
    healed = SharedRateLimiter(tmp_path, rate_bps=16 * MIB, burst_bytes=64 * 1024)
    healed.acquire("10.0.0.2", 1024)  # does not raise
    state = json.loads(limiter.state_path.read_text())
    assert "10.0.0.2" in state["ips"]
