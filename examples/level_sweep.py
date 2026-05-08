"""Sweep OmniComp and zstd levels 1..22 under identical conditions.

Outputs:
  - docs/level_sweep.csv
  - docs/level_sweep.md
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zstandard as zstd
from omnicomp import compress as omni_compress, decompress as omni_decompress


SILESIA_FILES = [
    "dickens",
    "mozilla",
    "mr",
    "nci",
    "ooffice",
    "osdb",
    "reymont",
    "samba",
    "sao",
    "webster",
    "x-ray",
    "xml",
]


def mb(n: int) -> float:
    return n / (1024.0 * 1024.0)


def run_best(fn, repeats: int):
    best_t = float("inf")
    best_out = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t0
        if dt < best_t:
            best_t = dt
            best_out = out
    return best_out, best_t


def load_dataset(silesia_dir: Path, mode: str) -> bytes:
    if mode == "silesia_blob":
        return b"".join((silesia_dir / f).read_bytes() for f in SILESIA_FILES)
    if mode == "xray":
        return (silesia_dir / "x-ray").read_bytes()
    if mode == "mixed":
        blob = b"".join((silesia_dir / f).read_bytes() for f in SILESIA_FILES)
        # Add high-structure numeric synthetic payload to stress specialized codecs.
        numeric = bytearray()
        v = 0
        for i in range(2_000_000):
            v = (v + ((i % 7) - 3)) & 0xFFFFFFFF
            numeric += int(v).to_bytes(4, "little", signed=False)
        return blob + bytes(numeric)
    raise ValueError(f"Unknown mode: {mode}")


def sweep(data: bytes, repeats: int, threads: int):
    rows = []
    n_mb = mb(len(data))
    for level in range(1, 23):
        # OmniComp
        oc, t_oc = run_best(lambda l=level: omni_compress(data, level=l, n_threads=threads), repeats)
        ob, t_od = run_best(lambda c=oc: omni_decompress(c), repeats)
        assert ob == data

        # zstd
        cctx = zstd.ZstdCompressor(level=level, threads=0)
        dctx = zstd.ZstdDecompressor()
        zc, t_zc = run_best(lambda c=cctx: c.compress(data), repeats)
        zb, t_zd = run_best(lambda c=zc, d=dctx: d.decompress(c), repeats)
        assert zb == data

        rows.append(
            {
                "level": level,
                "ratio_omni": len(data) / max(1, len(oc)),
                "ratio_zstd": len(data) / max(1, len(zc)),
                "comp_omni_mb_s": n_mb / t_oc,
                "comp_zstd_mb_s": n_mb / t_zc,
                "decomp_omni_mb_s": n_mb / t_od,
                "decomp_zstd_mb_s": n_mb / t_zd,
            }
        )
        print(
            f"L{level:02d}  "
            f"Omni ratio={rows[-1]['ratio_omni']:.3f} c={rows[-1]['comp_omni_mb_s']:.1f} d={rows[-1]['decomp_omni_mb_s']:.1f} | "
            f"zstd ratio={rows[-1]['ratio_zstd']:.3f} c={rows[-1]['comp_zstd_mb_s']:.1f} d={rows[-1]['decomp_zstd_mb_s']:.1f}"
        )
    return rows


def write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "level",
                "ratio_omni",
                "ratio_zstd",
                "comp_omni_mb_s",
                "comp_zstd_mb_s",
                "decomp_omni_mb_s",
                "decomp_zstd_mb_s",
                "ratio_delta_pct_omni_vs_zstd",
                "decomp_delta_pct_omni_vs_zstd",
            ]
        )
        for r in rows:
            ratio_delta = ((r["ratio_omni"] / r["ratio_zstd"]) - 1.0) * 100.0
            decomp_delta = ((r["decomp_omni_mb_s"] / r["decomp_zstd_mb_s"]) - 1.0) * 100.0
            w.writerow(
                [
                    r["level"],
                    f"{r['ratio_omni']:.6f}",
                    f"{r['ratio_zstd']:.6f}",
                    f"{r['comp_omni_mb_s']:.6f}",
                    f"{r['comp_zstd_mb_s']:.6f}",
                    f"{r['decomp_omni_mb_s']:.6f}",
                    f"{r['decomp_zstd_mb_s']:.6f}",
                    f"{ratio_delta:.6f}",
                    f"{decomp_delta:.6f}",
                ]
            )


def write_md(path: Path, mode: str, n_bytes: int, rows):
    lines = []
    lines.append(f"# Level Sweep ({mode})")
    lines.append("")
    lines.append(f"- Dataset size: `{mb(n_bytes):.2f} MB`")
    lines.append("")
    lines.append("| Level | Omni Ratio | zstd Ratio | Omni C MB/s | zstd C MB/s | Omni D MB/s | zstd D MB/s | Ratio Delta % | Decomp Delta % |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        ratio_delta = ((r["ratio_omni"] / r["ratio_zstd"]) - 1.0) * 100.0
        decomp_delta = ((r["decomp_omni_mb_s"] / r["decomp_zstd_mb_s"]) - 1.0) * 100.0
        lines.append(
            f"| {r['level']} | {r['ratio_omni']:.3f} | {r['ratio_zstd']:.3f} | "
            f"{r['comp_omni_mb_s']:.1f} | {r['comp_zstd_mb_s']:.1f} | "
            f"{r['decomp_omni_mb_s']:.1f} | {r['decomp_zstd_mb_s']:.1f} | "
            f"{ratio_delta:+.2f}% | {decomp_delta:+.2f}% |"
        )
    lines.append("")

    # Best decompression choices with ratio guard.
    lines.append("## Decompression-First Picks")
    lines.append("")
    best = []
    for r in rows:
        ratio_delta = ((r["ratio_omni"] / r["ratio_zstd"]) - 1.0) * 100.0
        decomp_delta = ((r["decomp_omni_mb_s"] / r["decomp_zstd_mb_s"]) - 1.0) * 100.0
        if ratio_delta >= -3.0:
            best.append((decomp_delta, r["level"], ratio_delta))
    best.sort(reverse=True)
    if best:
        for decomp_delta, lvl, ratio_delta in best[:5]:
            lines.append(
                f"- `L{lvl}`: decomp `{decomp_delta:+.2f}%` vs zstd at same level, ratio `{ratio_delta:+.2f}%` vs zstd."
            )
    else:
        lines.append("- No level met the ratio guard.")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--silesia-dir", default="/tmp/silesia")
    ap.add_argument("--mode", choices=["silesia_blob", "xray", "mixed"], default="silesia_blob")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--csv-output", default="docs/level_sweep.csv")
    ap.add_argument("--md-output", default="docs/level_sweep.md")
    args = ap.parse_args()

    data = load_dataset(Path(args.silesia_dir), args.mode)
    rows = sweep(data, repeats=args.repeats, threads=args.threads)
    write_csv(Path(args.csv_output), rows)
    write_md(Path(args.md_output), args.mode, len(data), rows)
    print(f"\nWrote {args.csv_output}")
    print(f"Wrote {args.md_output}")


if __name__ == "__main__":
    main()
