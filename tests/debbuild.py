"""Build real Debian packages for tests without extra dependencies."""

from __future__ import annotations

import argparse
import io
import random
import tarfile
from pathlib import Path

DEFAULT_CONTROL = {
    "Package": "demo",
    "Version": "1.0.0",
    "Architecture": "amd64",
    "Maintainer": "Test <test@example.com>",
    "Section": "misc",
    "Priority": "optional",
    "Description": "demo package",
}


def sized_payload(size: int, seed: int) -> bytes:
    """Deterministic pseudo-random payload of *size* bytes.

    Random bytes barely compress, so the resulting ``.deb`` is about as large
    as the payload -- exactly what the download-rate verification needs.
    """

    return random.Random(seed).randbytes(size)


def _tar_gz(members: dict[str, bytes], mtime: int = 0) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = (
            f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        )
        assert len(header) == 60, header
        out += header + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def build_deb(
    path: Path,
    *,
    control: dict[str, str] | None = None,
    payload: bytes = b"payload\n",
    payload_size: int | None = None,
    seed: int = 0,
    arch: str = "amd64",
) -> Path:
    fields = dict(DEFAULT_CONTROL)
    fields["Architecture"] = arch
    if control:
        fields.update(control)
    if payload_size is not None:
        payload = sized_payload(payload_size, seed)
    control_text = "".join(f"{key}: {value}\n" for key, value in fields.items())
    control_tar = _tar_gz(
        {"control": control_text.encode(), "md5sums": b""}
    )
    data_tar = _tar_gz(
        {f"usr/share/doc/{fields['Package']}/README": payload},
    )
    blob = _ar(
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", control_tar),
            ("data.tar.gz", data_tar),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


def _parse_size(text: str) -> int:
    suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3}
    if text and text[-1].upper() in suffixes:
        return int(text[:-1]) * suffixes[text[-1].upper()]
    return int(text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a real .deb, optionally with a large pseudo-random payload."
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--size", type=_parse_size, default=None,
                        help="payload bytes, e.g. 8388608 or 8M")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    out = build_deb(
        args.out,
        control={"Package": args.package, "Version": args.version},
        payload_size=args.size,
        seed=args.seed,
    )
    print(out)


if __name__ == "__main__":
    main()
