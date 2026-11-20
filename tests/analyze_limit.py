"""Checks for the shared-rate-limit verification runs.

Subcommands
-----------
``bound``       every time window: total bytes <= rate*dt + burst (+slack)
``rate``        bytes/second inside a (trimmed or explicit) window
``hash``        a finished download matches the published SHA-256 and size
``incomplete``  a download was cut short (expected when a server is killed)

Samples are ``{"t": <wall clock>, "bytes": <cumulative>}`` JSONL files
written by ``loadgen.py``; containers and host share one clock, so samples
from different machines align.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
from pathlib import Path


def _load(path: str) -> tuple[list[float], list[int]]:
    stamps: list[float] = []
    counts: list[int] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        stamps.append(float(rec["t"]))
        counts.append(int(rec["bytes"]))
    if not stamps:
        raise SystemExit(f"{path}: no samples")
    return stamps, counts


def _value_at(stamps: list[float], counts: list[int], moment: float) -> int:
    idx = bisect.bisect_right(stamps, moment) - 1
    return counts[idx] if idx >= 0 else 0


def _load_all(files: list[str]) -> list[tuple[list[float], list[int]]]:
    return [_load(path) for path in files]


def cmd_bound(args: argparse.Namespace) -> int:
    series = _load_all(args.files)
    events = sorted({t for stamps, _ in series for t in stamps})
    totals = [
        (t, sum(_value_at(stamps, counts, t) for stamps, counts in series))
        for t in events
    ]
    slack_bytes = args.slack * args.rate
    worst_excess = 0.0
    worst_window: tuple[float, float] | None = None
    for i, (t0, b0) in enumerate(totals):
        for t1, b1 in totals[i + 1 :]:
            allowed = args.rate * (t1 - t0) + args.burst + slack_bytes
            excess = (b1 - b0) - allowed
            if excess > worst_excess:
                worst_excess = excess
                worst_window = (t0, t1)
    status = "PASS" if worst_excess <= 0 else "FAIL"
    print(
        f"[{status}] bound: worst excess {worst_excess:.0f} B "
        f"(rate={args.rate} B/s, burst={args.burst} B, slack={slack_bytes:.0f} B)"
        + (f" in window {worst_window}" if worst_window else "")
    )
    return 0 if worst_excess <= 0 else 1


def cmd_rate(args: argparse.Namespace) -> int:
    series = _load_all(args.files)
    t0 = args.t0
    if t0 is None:
        t0 = max(stamps[0] for stamps, _ in series) + args.trim_start
    t1 = args.t1
    if t1 is None:
        t1 = min(stamps[-1] for stamps, _ in series) - args.trim_end
    if t1 <= t0:
        print(f"[FAIL] rate: empty window [{t0}, {t1}]")
        return 1
    moved = sum(
        _value_at(stamps, counts, t1) - _value_at(stamps, counts, t0)
        for stamps, counts in series
    )
    rate = moved / (t1 - t0)
    ok = True
    if args.min_rate is not None and rate < args.min_rate:
        ok = False
    if args.max_rate is not None and rate > args.max_rate:
        ok = False
    status = "PASS" if ok else "FAIL"
    print(
        f"[{status}] rate: {rate:.0f} B/s in [{t0:.3f}, {t1:.3f}] "
        f"(want >= {args.min_rate}, <= {args.max_rate})"
    )
    return 0 if ok else 1


def _summary(path: str) -> dict:
    return json.loads(Path(path).read_text())


def cmd_hash(args: argparse.Namespace) -> int:
    summary = _summary(args.summary)
    problems = []
    if summary.get("status") != 200:
        problems.append(f"status={summary.get('status')}")
    if summary.get("error"):
        problems.append(f"error={summary['error']}")
    if summary.get("sha256") != args.sha256:
        problems.append(
            f"sha256={summary.get('sha256')} != expected {args.sha256}"
        )
    if args.size is not None and summary.get("bytes") != args.size:
        problems.append(f"bytes={summary.get('bytes')} != expected {args.size}")
    status = "PASS" if not problems else "FAIL"
    print(f"[{status}] hash: {args.summary} " + ("; ".join(problems) or "ok"))
    return 0 if not problems else 1


def cmd_incomplete(args: argparse.Namespace) -> int:
    summary = _summary(args.summary)
    length = summary.get("content_length")
    cut = summary.get("error") or (
        isinstance(length, int) and summary.get("bytes", 0) < length
    )
    status = "PASS" if cut else "FAIL"
    print(
        f"[{status}] incomplete: {args.summary} "
        f"bytes={summary.get('bytes')}/{length} error={summary.get('error')}"
    )
    return 0 if cut else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bound", help="check rate*dt + burst for every window")
    p.add_argument("--rate", type=float, required=True)
    p.add_argument("--burst", type=float, required=True)
    p.add_argument("--slack", type=float, default=0.2,
                   help="extra allowance, in seconds worth of rate, to absorb "
                        "sampling quantisation")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_bound)

    p = sub.add_parser("rate", help="check bytes/second inside a window")
    p.add_argument("--t0", type=float, default=None, help="window start (wall)")
    p.add_argument("--t1", type=float, default=None, help="window end (wall)")
    p.add_argument("--trim-start", type=float, default=0.75)
    p.add_argument("--trim-end", type=float, default=0.5)
    p.add_argument("--min-rate", type=float, default=None)
    p.add_argument("--max-rate", type=float, default=None)
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_rate)

    p = sub.add_parser("hash", help="check a finished download")
    p.add_argument("--summary", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--size", type=int, default=None)
    p.set_defaults(func=cmd_hash)

    p = sub.add_parser("incomplete", help="check a download was cut short")
    p.add_argument("--summary", required=True)
    p.set_defaults(func=cmd_incomplete)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
