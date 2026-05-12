"""Command-line interface for the ``zxc`` compressor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnicomp import compress, decompress


def _default_compressed_path(path: Path) -> Path:
    return path.parent / (path.name + ".zxc")


def _default_decompressed_path(path: Path) -> Path:
    name = path.name
    if name.endswith(".zxc"):
        return path.parent / name[:-4]
    return path.with_suffix(".out")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zxc",
        description="OmniComp lossless compression (OMN8 container).",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="input file path",
    )
    parser.add_argument(
        "-d",
        "--decompress",
        action="store_true",
        help="decompress instead of compress",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="output path (default: input plus .zxc, or strip .zxc when decompressing)",
    )
    parser.add_argument(
        "-l",
        "--level",
        type=int,
        default=3,
        metavar="N",
        help="compression level 1..22 (default: 3); ignored when decompressing",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        default=0,
        metavar="N",
        help="thread count (0 = auto; default: 0)",
    )
    args = parser.parse_args(argv)

    inp = args.path
    if not inp.is_file():
        print(f"zxc: not a file: {inp}", file=sys.stderr)
        return 1

    try:
        data = inp.read_bytes()
    except OSError as e:
        print(f"zxc: read {inp}: {e}", file=sys.stderr)
        return 1

    if args.decompress:
        out = args.output if args.output is not None else _default_decompressed_path(inp)
        try:
            raw = decompress(data)
        except Exception as e:
            print(f"zxc: decompress failed: {e}", file=sys.stderr)
            return 1
    else:
        if not (1 <= args.level <= 22):
            print("zxc: level must be between 1 and 22", file=sys.stderr)
            return 1
        out = args.output if args.output is not None else _default_compressed_path(inp)
        raw = compress(data, level=args.level, n_threads=args.threads)

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
    except OSError as e:
        print(f"zxc: write {out}: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
