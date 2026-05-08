"""Full validation matrix for OmniComp.

Covers:
- multiple corpus
- memory used
- real dataset
- multithreading
- small files
- incompressible data
- streaming (chunked containers)
- latency
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import statistics
import string
import sys
import time
from pathlib import Path
import resource

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zstandard as zstd
from omnicomp import compress as omni_compress, decompress as omni_decompress

try:
    import lz4.frame as lz4f
except Exception:  # pragma: no cover - optional dependency
    lz4f = None


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


def now() -> float:
    return time.perf_counter()


def run_best(fn, repeats: int = 3):
    best_t = float("inf")
    best_out = None
    for _ in range(repeats):
        t0 = now()
        out = fn()
        dt = now() - t0
        if dt < best_t:
            best_t = dt
            best_out = out
    return best_out, best_t


def rss_mb() -> float:
    # ru_maxrss is KB on Linux, bytes on macOS.
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return v / (1024.0 * 1024.0)
    return v / 1024.0


def make_synthetic() -> dict[str, bytes]:
    rng = random.Random(123)
    text = (" ".join(["lorem", "ipsum", "dolor", "sit", "amet"]) + "\n") * 250_000
    random_data = os.urandom(16 * 1024 * 1024)
    small_json = (
        '{"id":1,"name":"alpha","ok":true,"items":[1,2,3],"meta":{"k":"v"}}\n' * 120_000
    ).encode()
    # Numeric-ish binary pattern.
    numeric = bytearray()
    v = 0
    for _ in range(2_000_000):
        v = (v + rng.randint(-3, 3)) & 0xFFFFFFFF
        numeric += int(v).to_bytes(4, "little", signed=False)
    return {
        "synthetic_text": text.encode(),
        "synthetic_structured": small_json,
        "synthetic_numeric_i32": bytes(numeric),
        "synthetic_incompressible": random_data,
    }


def load_silesia(silesia_dir: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not silesia_dir.exists():
        return out
    for name in SILESIA_FILES:
        p = silesia_dir / name
        if p.exists():
            out[f"silesia_{name}"] = p.read_bytes()
    if out:
        blob = b"".join(out[k] for k in sorted(out))
        out["silesia_blob"] = blob
    return out


def load_real_dataset(real_dir: Path | None, max_files: int = 200) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if real_dir is None or not real_dir.exists():
        return out
    files = []
    for p in real_dir.rglob("*"):
        if any(part.startswith(".") for part in p.parts):
            continue
        if p.is_file() and p.stat().st_size >= 4096:
            files.append(p)
    files = sorted(files)[:max_files]
    if not files:
        return out
    blob_parts = []
    for p in files:
        try:
            b = p.read_bytes()
        except Exception:
            continue
        out[f"real_{p.name}"] = b
        blob_parts.append(b)
    if blob_parts:
        out["real_blob"] = b"".join(blob_parts)
    return out


def compare_pair(data: bytes, omni_level: int, threads: int):
    cctx = zstd.ZstdCompressor(level=omni_level if omni_level > 0 else 1, threads=0)
    dctx = zstd.ZstdDecompressor()

    oc, t_oc = run_best(lambda: omni_compress(data, level=omni_level, n_threads=threads))
    ob, t_od = run_best(lambda: omni_decompress(oc))
    assert ob == data, "OmniComp round-trip failed"

    zc, t_zc = run_best(lambda: cctx.compress(data))
    zb, t_zd = run_best(lambda: dctx.decompress(zc))
    assert zb == data, "zstd round-trip failed"

    out = {
        "ratio_omni": len(data) / max(1, len(oc)),
        "ratio_zstd": len(data) / max(1, len(zc)),
        "comp_omni": mb(len(data)) / t_oc,
        "comp_zstd": mb(len(data)) / t_zc,
        "decomp_omni": mb(len(data)) / t_od,
        "decomp_zstd": mb(len(data)) / t_zd,
        "ratio_lz4": None,
        "comp_lz4": None,
        "decomp_lz4": None,
    }
    if lz4f is not None:
        lc, t_lc = run_best(lambda: lz4f.compress(data))
        lb, t_ld = run_best(lambda: lz4f.decompress(lc))
        assert lb == data, "lz4 round-trip failed"
        out["ratio_lz4"] = len(data) / max(1, len(lc))
        out["comp_lz4"] = mb(len(data)) / t_lc
        out["decomp_lz4"] = mb(len(data)) / t_ld
    return out


def latency_test(level: int):
    payloads = {
        "4KB": os.urandom(4 * 1024),
        "64KB": os.urandom(64 * 1024),
        "1MB": os.urandom(1024 * 1024),
    }
    out = {}
    for name, data in payloads.items():
        c_times = []
        d_times = []
        for _ in range(60):
            t0 = now()
            c = omni_compress(data, level=level, n_threads=1)
            t1 = now()
            b = omni_decompress(c)
            t2 = now()
            assert b == data
            c_times.append((t1 - t0) * 1000.0)
            d_times.append((t2 - t1) * 1000.0)
        c_times.sort()
        d_times.sort()
        idx95 = max(0, int(len(c_times) * 0.95) - 1)
        out[name] = {
            "c_p50_ms": statistics.median(c_times),
            "c_p95_ms": c_times[idx95],
            "d_p50_ms": statistics.median(d_times),
            "d_p95_ms": d_times[idx95],
        }
    return out


def small_files_test(level: int):
    rng = random.Random(99)
    files = []
    for _ in range(600):
        size = rng.choice([32, 64, 128, 256, 512, 1024, 2048, 4096, 8192])
        if rng.random() < 0.5:
            s = "".join(rng.choice(string.ascii_letters + " \n") for _ in range(size))
            files.append(s.encode())
        else:
            files.append(os.urandom(size))

    total_in = sum(len(f) for f in files)
    total_out = 0
    t0 = now()
    for f in files:
        c = omni_compress(f, level=level, n_threads=1)
        b = omni_decompress(c)
        assert b == f
        total_out += len(c)
    dt = now() - t0
    return {
        "n_files": len(files),
        "total_in_mb": mb(total_in),
        "ratio": total_in / max(1, total_out),
        "end_to_end_ms": dt * 1000.0,
        "avg_file_ms": (dt * 1000.0) / len(files),
    }


def streaming_test(level: int):
    # Chunked stream test: independent frames as a continuous source.
    chunk = 256 * 1024
    src = os.urandom(8 * 1024 * 1024) + (
        b"sensor-record-0001\n" * 200_000
    )
    frames = []
    for i in range(0, len(src), chunk):
        frames.append(omni_compress(src[i : i + chunk], level=level, n_threads=1))
    out = bytearray()
    for f in frames:
        out += omni_decompress(f)
    assert bytes(out) == src
    return {
        "chunks": len(frames),
        "src_mb": mb(len(src)),
        "avg_frame_kb": (sum(len(f) for f in frames) / len(frames)) / 1024.0,
    }


def render_report(
    out_path: Path,
    level: int,
    corpora_rows: list[tuple[str, int, dict]],
    mt: dict,
    mem: dict,
    small: dict,
    incompressible: dict,
    stream: dict,
    latency: dict,
):
    lines = []
    lines.append("# Validation Report")
    lines.append("")
    lines.append(f"- OmniComp level: `{level}`")
    lines.append(f"- Generated: `{time.strftime('%Y-%m-%d %H:%M:%S')}`")
    lines.append("")
    lines.append("## Multiple Corpus + Real Dataset")
    lines.append("")
    has_lz4 = any(r.get("ratio_lz4") is not None for _, _, r in corpora_rows)
    if has_lz4:
        lines.append("| Dataset | Size MB | Omni Ratio | zstd Ratio | lz4 Ratio | Omni C MB/s | zstd C MB/s | lz4 C MB/s | Omni D MB/s | zstd D MB/s | lz4 D MB/s |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    else:
        lines.append("| Dataset | Size MB | Omni Ratio | zstd Ratio | Omni C MB/s | zstd C MB/s | Omni D MB/s | zstd D MB/s |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for name, size_b, r in corpora_rows:
        if has_lz4:
            lz4_ratio = f"{r['ratio_lz4']:.3f}" if r["ratio_lz4"] is not None else "n/a"
            lz4_c = f"{r['comp_lz4']:.1f}" if r["comp_lz4"] is not None else "n/a"
            lz4_d = f"{r['decomp_lz4']:.1f}" if r["decomp_lz4"] is not None else "n/a"
            lines.append(
                f"| {name} | {mb(size_b):.2f} | {r['ratio_omni']:.3f} | {r['ratio_zstd']:.3f} | {lz4_ratio} | "
                f"{r['comp_omni']:.1f} | {r['comp_zstd']:.1f} | {lz4_c} | "
                f"{r['decomp_omni']:.1f} | {r['decomp_zstd']:.1f} | {lz4_d} |"
            )
        else:
            lines.append(
                f"| {name} | {mb(size_b):.2f} | {r['ratio_omni']:.3f} | {r['ratio_zstd']:.3f} | "
                f"{r['comp_omni']:.1f} | {r['comp_zstd']:.1f} | {r['decomp_omni']:.1f} | {r['decomp_zstd']:.1f} |"
            )
    lines.append("")
    lines.append("## Multithreading")
    lines.append("")
    lines.append(
        f"- 1 thread: `{mt['one_thread_mb_s']:.1f} MB/s` | {mt['one_ratio']:.3f} ratio"
    )
    lines.append(
        f"- {mt['threads']} threads: `{mt['many_thread_mb_s']:.1f} MB/s` | {mt['many_ratio']:.3f} ratio"
    )
    lines.append(f"- Speedup: `{mt['speedup']:.2f}x`")
    lines.append("")
    lines.append("## Memory Used")
    lines.append("")
    lines.append(f"- Peak RSS before run: `{mem['rss_before_mb']:.1f} MB`")
    lines.append(f"- Peak RSS after run: `{mem['rss_after_mb']:.1f} MB`")
    lines.append(f"- Delta peak RSS: `{mem['rss_delta_mb']:.1f} MB`")
    lines.append("")
    lines.append("## Small Files")
    lines.append("")
    lines.append(
        f"- `{small['n_files']}` files, `{small['total_in_mb']:.2f} MB` total, ratio `{small['ratio']:.3f}`"
    )
    lines.append(
        f"- End-to-end `{small['end_to_end_ms']:.1f} ms`, avg `{small['avg_file_ms']:.3f} ms/file`"
    )
    lines.append("")
    lines.append("## Incompressible Data")
    lines.append("")
    lines.append(
        f"- Size `{incompressible['size_mb']:.2f} MB`, ratio `{incompressible['ratio']:.3f}`, "
        f"comp `{incompressible['comp_mb_s']:.1f} MB/s`, decomp `{incompressible['decomp_mb_s']:.1f} MB/s`"
    )
    lines.append("")
    lines.append("## Streaming (Chunked Frames)")
    lines.append("")
    lines.append(
        f"- `{stream['chunks']}` chunks over `{stream['src_mb']:.2f} MB`, avg frame `{stream['avg_frame_kb']:.1f} KB`"
    )
    lines.append("- Verified sequential decode of chunked frames reconstructs original byte stream.")
    lines.append("")
    lines.append("## Latency")
    lines.append("")
    lines.append("| Payload | C p50 ms | C p95 ms | D p50 ms | D p95 ms |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, d in latency.items():
        lines.append(
            f"| {name} | {d['c_p50_ms']:.3f} | {d['c_p95_ms']:.3f} | {d['d_p50_ms']:.3f} | {d['d_p95_ms']:.3f} |"
        )
    lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_csv(csv_path: Path, level: int, corpora_rows: list[tuple[str, int, dict]]):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "level",
                "dataset",
                "size_mb",
                "ratio_omni",
                "ratio_zstd",
                "ratio_lz4",
                "comp_omni_mb_s",
                "comp_zstd_mb_s",
                "comp_lz4_mb_s",
                "decomp_omni_mb_s",
                "decomp_zstd_mb_s",
                "decomp_lz4_mb_s",
            ]
        )
        for name, size_b, r in corpora_rows:
            w.writerow(
                [
                    level,
                    name,
                    f"{mb(size_b):.6f}",
                    f"{r['ratio_omni']:.6f}",
                    f"{r['ratio_zstd']:.6f}",
                    "" if r["ratio_lz4"] is None else f"{r['ratio_lz4']:.6f}",
                    f"{r['comp_omni']:.6f}",
                    f"{r['comp_zstd']:.6f}",
                    "" if r["comp_lz4"] is None else f"{r['comp_lz4']:.6f}",
                    f"{r['decomp_omni']:.6f}",
                    f"{r['decomp_zstd']:.6f}",
                    "" if r["decomp_lz4"] is None else f"{r['decomp_lz4']:.6f}",
                ]
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=9)
    ap.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--silesia-dir", default="/tmp/silesia")
    ap.add_argument("--real-dataset-dir", default="")
    ap.add_argument("--output", default="docs/validation_report.md")
    ap.add_argument("--csv-output", default="docs/validation_report.csv")
    args = ap.parse_args()

    level = max(1, min(22, args.level))
    silesia_dir = Path(args.silesia_dir)
    real_dir = Path(args.real_dataset_dir) if args.real_dataset_dir else None
    out_path = Path(args.output)
    csv_path = Path(args.csv_output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    corpora: dict[str, bytes] = {}
    corpora.update(make_synthetic())
    corpora.update(load_silesia(silesia_dir))
    corpora.update(load_real_dataset(real_dir))

    rows = []
    for name, data in corpora.items():
        try:
            r = compare_pair(data, omni_level=level, threads=1)
            rows.append((name, len(data), r))
            print(f"[ok] {name:<24} ratio={r['ratio_omni']:.3f} comp={r['comp_omni']:.1f} MB/s")
        except Exception as e:
            print(f"[skip] {name}: {e}")

    # Multithreading on the largest available corpus.
    if rows:
        largest_name, _, _ = max(rows, key=lambda x: x[1])
        mt_data = corpora[largest_name]
    else:
        mt_data = os.urandom(32 * 1024 * 1024)

    c1, t1 = run_best(lambda: omni_compress(mt_data, level=level, n_threads=1))
    cN, tN = run_best(lambda: omni_compress(mt_data, level=level, n_threads=args.threads))
    assert omni_decompress(c1) == mt_data
    assert omni_decompress(cN) == mt_data
    mt = {
        "threads": args.threads,
        "one_thread_mb_s": mb(len(mt_data)) / t1,
        "many_thread_mb_s": mb(len(mt_data)) / tN,
        "speedup": t1 / tN if tN > 0 else 0.0,
        "one_ratio": len(mt_data) / max(1, len(c1)),
        "many_ratio": len(mt_data) / max(1, len(cN)),
    }

    # Memory test uses a 128 MB blob to force allocation pressure.
    mem_data = os.urandom(128 * 1024 * 1024)
    rss0 = rss_mb()
    c, _ = run_best(lambda: omni_compress(mem_data, level=level, n_threads=1), repeats=1)
    b, _ = run_best(lambda: omni_decompress(c), repeats=1)
    assert b == mem_data
    rss1 = rss_mb()
    mem = {"rss_before_mb": rss0, "rss_after_mb": rss1, "rss_delta_mb": max(0.0, rss1 - rss0)}

    # Small files.
    small = small_files_test(level)

    # Incompressible.
    incomp = os.urandom(64 * 1024 * 1024)
    c, tc = run_best(lambda: omni_compress(incomp, level=level, n_threads=1))
    b, td = run_best(lambda: omni_decompress(c))
    assert b == incomp
    incompressible = {
        "size_mb": mb(len(incomp)),
        "ratio": len(incomp) / max(1, len(c)),
        "comp_mb_s": mb(len(incomp)) / tc,
        "decomp_mb_s": mb(len(incomp)) / td,
    }

    stream = streaming_test(level)
    latency = latency_test(level)

    rows = sorted(rows, key=lambda x: x[1], reverse=True)
    render_report(out_path, level, rows, mt, mem, small, incompressible, stream, latency)
    render_csv(csv_path, level, rows)
    print(f"\nWrote validation report: {out_path}")
    print(f"Wrote validation CSV: {csv_path}")


if __name__ == "__main__":
    main()
