"""A byte-rate token bucket shared by independent processes.

Several pool-server processes serve the same APT repository export and must
enforce one common download budget, configured once in docker-compose
(``APT_REPO_RATE_BPS``).  The limiter state lives in a small JSON document on
a shared volume; every process reads and updates it under ``flock(2)``, so
the budget holds across processes and survives the death of any of them
(the lock is released by the kernel and the state file persists).

Guarantees
----------
* Hard cap: in any interval of length ``dt`` the bytes granted to *all*
  connections of *all* processes together never exceed
  ``rate * dt + burst`` -- a classic token bucket of capacity ``burst``
  (default 64 KiB) refilled at ``rate`` bytes/second.
* Fair sharing: the refill rate is split equally between *source IPs* that
  have body bytes to send.  An IP counts as active while it has requested
  tokens within the last ``active_ttl`` seconds, so the share of a departed
  peer (e.g. a SIGKILLed server process) becomes available to the survivors
  within that TTL -- 1 s by default, well inside the required 2 s.
* Several connections from the same source IP -- even to different server
  processes -- draw from that IP's single bucket, i.e. one IP = one share.

If the state file is ever torn (a writer killed mid-write), the next accessor
reinitialises it with a full bucket; this is the only path that can exceed
the cap, and only by one ``burst``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import threading
import time
from pathlib import Path
from typing import IO, Any, Iterator

#: Default aggregate byte rate when the environment does not set one.
DEFAULT_RATE_BPS = 100 * 1024 * 1024
#: Default bucket capacity; matches the permitted burst of the deployment.
DEFAULT_BURST_BYTES = 64 * 1024
#: How long an IP stays "active" (entitled to a share) after its last request.
DEFAULT_ACTIVE_TTL = 1.0

#: Entries not seen for this long are dropped from the state document.
_STALE_AFTER = 30.0
#: Sleep bounds between two locked grant attempts.
_MIN_SLEEP = 0.005
_MAX_SLEEP = 0.05


class SharedRateLimiter:
    """Blocking token-bucket client backed by a shared state file.

    One instance per process; all state lives in the file, so instances in
    different processes (and threads within a process) coordinate through
    ``flock`` on the state file itself.
    """

    def __init__(
        self,
        state_dir: str | Path,
        rate_bps: float,
        burst_bytes: int = DEFAULT_BURST_BYTES,
        active_ttl: float = DEFAULT_ACTIVE_TTL,
    ) -> None:
        if rate_bps <= 0:
            raise ValueError("rate_bps must be positive")
        if burst_bytes <= 0:
            raise ValueError("burst_bytes must be positive")
        if active_ttl <= 0:
            raise ValueError("active_ttl must be positive")
        self._rate = float(rate_bps)
        self._burst = float(burst_bytes)
        self._ttl = float(active_ttl)
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / "state.json"
        # One descriptor per process: the threading lock serialises the
        # seek/read/write sequence between our own threads, flock serialises
        # against the other processes.
        self._fh: IO[bytes] = self._path.open("a+b")
        self._lock = threading.Lock()

    @property
    def state_path(self) -> Path:
        return self._path

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "global": {"tokens": self._burst, "ts": time.time()},
            "ips": {},
        }

    def _read_state(self) -> dict[str, Any]:
        self._fh.seek(0)
        raw = self._fh.read()
        try:
            state = json.loads(raw.decode("utf-8"))
            bucket = state["global"]
            bucket["tokens"] = float(bucket["tokens"])
            bucket["ts"] = float(bucket["ts"])
            ips = state["ips"]
            if not isinstance(ips, dict):
                raise ValueError("bad ips")
            for entry in ips.values():
                entry["tokens"] = float(entry["tokens"])
                entry["ts"] = float(entry["ts"])
                entry["seen"] = float(entry["seen"])
            return state
        except Exception:
            # Missing (first use) or torn by a killed writer: start clean.
            return self._fresh_state()

    @contextlib.contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read_state()
                yield state
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(json.dumps(state).encode("utf-8"))
                self._fh.flush()
            finally:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)

    def _try_grant(self, ip: str, want: int) -> tuple[int, float]:
        """One locked attempt; returns ``(granted_bytes, suggested_sleep)``."""

        with self._locked_state() as state:
            now = time.time()
            ips = state["ips"]
            for key in [k for k, v in ips.items() if now - v["seen"] > _STALE_AFTER]:
                del ips[key]
            entry = ips.get(ip)
            if entry is None:
                entry = ips[ip] = {
                    "tokens": self._burst,
                    "ts": now,
                    "seen": now,
                }
            entry["seen"] = now

            active = [k for k, v in ips.items() if now - v["seen"] <= self._ttl]
            fair_rate = self._rate / max(1, len(active))

            bucket = state["global"]
            bucket["tokens"] = min(
                self._burst, bucket["tokens"] + (now - bucket["ts"]) * self._rate
            )
            bucket["ts"] = now
            for key in active:
                item = ips[key]
                item["tokens"] = min(
                    self._burst, item["tokens"] + (now - item["ts"]) * fair_rate
                )
                item["ts"] = now

            want = min(want, int(self._burst))
            grant = int(min(want, bucket["tokens"], entry["tokens"]))
            if grant >= 1:
                bucket["tokens"] -= grant
                entry["tokens"] -= grant
                return grant, 0.0

            wait_global = (
                (want - bucket["tokens"]) / self._rate
                if bucket["tokens"] < want
                else 0.0
            )
            wait_fair = (
                (want - entry["tokens"]) / fair_rate
                if entry["tokens"] < want
                else 0.0
            )
            pause = min(max(max(wait_global, wait_fair), _MIN_SLEEP), _MAX_SLEEP)
            return 0, pause

    def acquire(self, source_ip: str, nbytes: int) -> None:
        """Block until *nbytes* may be sent to *source_ip* under the budget.

        Every granted byte is paid for from both the global bucket and the
        source IP's fair-share bucket at grant time; the caller must send at
        most the granted amount afterwards.
        """

        remaining = nbytes
        while remaining > 0:
            granted, pause = self._try_grant(source_ip, remaining)
            remaining -= granted
            if granted == 0:
                time.sleep(pause)
