"""HTTP download jobs with byte sampling, for shared-rate-limit verification.

Runs inside a client container (or anywhere with HTTP access to the pool
servers).  Each ``--job name=url`` is one HTTP GET executed in its own
thread; while downloading, ``(wall-clock, cumulative body bytes)`` samples
are recorded.  Per job two files are written below ``--out-dir``:

``<prefix>.<name>.samples.jsonl``   one ``{"t": ..., "bytes": ...}`` per line
``<prefix>.<name>.json``            summary: status, bytes, SHA-256, error

Wall-clock timestamps make samples comparable across containers (they share
the host clock).  The process exit code is always 0; the summaries are the
source of truth (a killed server shows up as a job ``error``).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


def _run_job(
    name: str,
    url: str,
    out_dir: Path,
    prefix: str,
    source_ip: str | None,
    sample_interval: float,
) -> None:
    parts = urlsplit(url)
    summary: dict[str, object] = {
        "job": name,
        "url": url,
        "status": None,
        "error": None,
        "content_length": None,
        "bytes": 0,
        "sha256": None,
        "t0_wall": None,
        "t1_wall": None,
    }
    samples: list[tuple[float, int]] = []
    total = 0
    conn = http.client.HTTPConnection(
        parts.hostname,
        parts.port or 80,
        timeout=60,
        source_address=(source_ip, 0) if source_ip else None,
    )
    try:
        target = parts.path + (("?" + parts.query) if parts.query else "")
        conn.request("GET", target)
        resp = conn.getresponse()
        summary["status"] = resp.status
        length = resp.getheader("Content-Length")
        summary["content_length"] = int(length) if length is not None else None
        sha = hashlib.sha256()
        t0 = time.time()
        summary["t0_wall"] = t0
        samples.append((t0, 0))
        last = t0
        if resp.status != 200:
            resp.read()
            summary["error"] = f"unexpected HTTP status {resp.status}"
        else:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                sha.update(chunk)
                total += len(chunk)
                now = time.time()
                if now - last >= sample_interval:
                    samples.append((now, total))
                    last = now
            summary["sha256"] = sha.hexdigest()
    except Exception as exc:  # e.g. connection reset when a server is killed
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        summary["t1_wall"] = time.time()
        summary["bytes"] = total
        if samples and samples[-1][1] != total:
            samples.append((summary["t1_wall"], total))
        conn.close()

    samples_path = out_dir / f"{prefix}.{name}.samples.jsonl"
    with samples_path.open("w") as fh:
        for stamp, count in samples:
            fh.write(json.dumps({"t": stamp, "bytes": count}) + "\n")
    summary_path = out_dir / f"{prefix}.{name}.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"job {name}: status={summary['status']} bytes={summary['bytes']} "
        f"error={summary['error']}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--source-ip", default=None,
                        help="bind this local source address (default: OS-chosen)")
    parser.add_argument("--sample-interval", type=float, default=0.05)
    parser.add_argument("--job", action="append", required=True, metavar="NAME=URL")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    threads = []
    for spec in args.job:
        name, sep, url = spec.partition("=")
        if not sep or not name:
            parser.error(f"invalid --job {spec!r}, expected name=url")
        threads.append(
            threading.Thread(
                target=_run_job,
                args=(name, url, args.out_dir, args.prefix,
                      args.source_ip, args.sample_interval),
            )
        )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    sys.exit(main())
