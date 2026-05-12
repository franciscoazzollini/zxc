# Test And Validation Matrix

This project uses a validation matrix that explicitly covers:

- Multiple corpus
- Memory used
- Real dataset
- Multithreading
- Small files
- Incompressible data
- Streaming
- Latency

Run it with:

```bash
python3 examples/validation_matrix.py --level 9 --threads 8 \
  --silesia-dir /tmp/silesia \
  --real-dataset-dir /path/to/your/real_dataset \
  --output docs/validation_report.md \
  --csv-output docs/validation_report.csv
```

If `--real-dataset-dir` is omitted, synthetic datasets plus Silesia (if present) are used.

The generated `docs/validation_report.md` includes:

- Per-corpus ratio and throughput comparison against zstd and lz4
- Thread scaling and speedup
- Peak RSS delta (process-level)
- Small file end-to-end timing
- Incompressible-data behavior
- Chunked streaming round-trip verification
- p50/p95 compression and decompression latency

CSV export:

- `docs/validation_report.csv` contains one row per dataset under identical
  benchmark conditions (same input, same repeats, single-thread references)
  for OmniComp, zstd, and lz4.

Recommended release gate:

1. `python3 tests/test_roundtrip.py`
2. `python3 tests/test_validation_matrix.py`
3. `python3 examples/benchmark_silesia.py` (writes `docs/silesia_vs_zstd_metrics.csv` and `docs/silesia_per_file.csv` with default **1** compression thread; leading columns are deltas for ratio, decomp MB/s, and decomp wall time vs zstd — see script docstring). Use `--thread-configs N` or `--threads N` for multi-threaded compression.
4. `python3 examples/validation_matrix.py --level 9 --threads 8`
5. Save the generated report and add a short note to the paper addendum.

### AArch64 / NEON (shuffle k=4 decode)

When `libomnicomp_pipeline` is built for AArch64 and `uname -m` reports
`arm64` or `aarch64`, inverse byte-shuffle for **k = 4** may use NEON (`vst4`)
after the inner zstd frame. Other strides and non-ARM builds use the scalar
decoder only (bit-identical). See README **“AArch64 decode path (NEON)”** for
the runtime gate and when to expect a measurable speedup.
