# OmniComp v0.9

**A lossless adaptive multi-niche compressor that fills in the gaps between zstd levels.**

OmniComp routes each block to a specialised codec (predictor + shuffle for
floats, byte-shuffle for stride-structured data, RLE for runs, zstd for
text and generic content) and falls back to plain zstd whenever the
specialised codec under-performs. The pipeline is 100 % C; the Python
layer only assembles the self-describing container.

- **Lossless** — bit-exact round-trip verified on all 12 files of the
  [Silesia corpus](https://sun.aei.polsl.pl/~sdeor/index.php?page=silesia)
  plus a per-niche synthetic test suite.
- **A real Pareto frontier** with several operating points that beat
  zstd: `level=12` reaches **zstd-15-class ratio at 4.8× the compression
  speed**; `level=22` is **47 % faster than zstd-22**; every level
  decompresses **5–25 % faster** than zstd at the equivalent ratio.
- **MIT licensed**, depends only on libzstd and libpthread.

## Conclusions

OmniComp is ready to publish as an MIT open-source compressor with clear scope.

- **What it does best**: decompression-first workloads, numeric/structured
  binary data, and high-compression tiers where zstd levels 9..22 become
  expensive.
- **What the results show**:
  - On full Silesia at equal levels (1..22), OmniComp is usually within
    ~1-3% ratio of zstd while often decompressing faster.
  - Best decompression gains on Silesia occur around levels 11-19, with
    measured wins up to about +39% vs zstd at the same level.
  - On numeric-heavy inputs (for example `x-ray`), OmniComp can beat zstd on
    both ratio and decompression speed.
  - On very fast generic settings (`level` 1-2), zstd remains stronger for
    pure compression throughput.
- **Practical recommendation**:
  - If your main KPI is fastest generic compression: use plain zstd.
  - If your KPI is decompression speed with strong ratio: use OmniComp
    levels 9-19.
  - If your data is numeric/scientific and repeatedly read: OmniComp is a
    strong default choice.

All claims above are backed by reproducible artifacts committed in `docs/`
(`validation_report.*`, `silesia_vs_zstd_metrics.csv`, `silesia_per_file.csv`)
and by executable test/benchmark scripts in `tests/` and `examples/`.

## Headline result — Silesia (202 MB), single thread

| Algorithm | Ratio | Comp MB/s | Decomp MB/s | vs zstd                          |
|-----------|------:|----------:|------------:|----------------------------------|
| zstd -1   | 2.895 |       793 |       1 839 |                                   |
| zstd -3   | 3.205 |       492 |       1 694 |                                   |
| zstd -9   | 3.588 |       115 |       1 843 |                                   |
| zstd -15  | 3.727 |        13 |       2 022 |                                   |
| zstd -22  | 4.054 |       3.6 |       1 767 |                                   |
| **OmniComp L1**  | 2.865 | 649 | 1 898 | tracks zstd -1 (+3 % decomp)        |
| **OmniComp L3**  | 3.118 | 483 | 1 850 | tracks zstd -3 (+9 % decomp)        |
| **OmniComp L6**  | 3.432 | 176 | 2 068 | between zstd -3 and zstd -9, +52 % comp vs zstd -9 |
| **OmniComp L9**  | 3.541 | 120 | **2 166** | matches zstd -9, **+18 % decomp**   |
| **OmniComp L12** | 3.603 |  64 | **2 248** | zstd -15-class ratio at **4.8× the comp speed** |
| **OmniComp L15** | 3.659 |  17 | 2 213 | tracks zstd -15 (+9 % decomp)       |
| **OmniComp L19** | 3.925 | 6.4 | 1 919 | between zstd -15 and zstd -22       |
| **OmniComp L22** | 3.927 | 5.3 | 1 919 | zstd -22 territory, **+47 % comp**, +9 % decomp |

Hardware: Apple Silicon (M-series), libzstd 1.5.6, OmniComp `cc -O3 -march=native`,
best of 3 runs via `examples/benchmark_silesia.py`. Ratio uses the
zstd convention (`original / compressed`, bigger is better).

The `level` knob (1..22) controls a single per-block strategy that
combines: which zstd level to use for plain-zstd niches, which level to
use inside the specialised codecs, and how aggressively the cascade
runs.

## What is OmniComp actually winning?

Two real workloads where OmniComp's design gives a clean speed/ratio
edge over plain zstd:

### 1. Stride-structured numeric data

`x-ray` (Silesia, 16-bit medical imaging, 8 MB):

| Algorithm | Ratio | Decomp MB/s |
|-----------|------:|------------:|
| zstd -3   | 1.39  |       1 053 |
| zstd -9   | 1.58  |         990 |
| **OmniComp L3** | **1.74** | **1 902** |

OmniComp picks `byte-shuffle 8` here automatically, splitting the data
into 8 streams that zstd then compresses much more effectively. The
ratio gain is **+25 % over zstd -3** and the decoder is **80 % faster**
because de-shuffle is a memcpy-class operation.

### 2. Strong-but-fast tier (replacing zstd -15 / -22)

If your workflow needs zstd-15-class compression but can't afford its
13 MB/s, `OmniComp L12` lands at:

- **3.603 ratio** (vs 3.727 for zstd -15, only 3 % behind)
- **64 MB/s compression** (5× faster than zstd -15)
- **2 248 MB/s decompression** (+11 % vs zstd -15)

The pattern repeats at the very top: `OmniComp L22` reaches **3.927**
ratio at **5.3 MB/s** — essentially zstd-22 territory at 47 % less wall
time.

## Architecture

`omnicomp/pipeline.c` is a single shared library that does
detection, codec dispatch, parallel block compression and assembly.

1. **Niche detector** (≈ 50 µs / block on 8 MB):
   - One pass over the block builds a 256-entry histogram + ASCII counters.
   - Sub-sampling to 16 KB (4 chunks of 4 KB) so detector cost is
     ~constant in input size.
   - Byte autocorrelation at strides 2 / 4 / 8 / 16 / 20.
   - NaN / Inf bit-pattern scan to disqualify pred-shuffle on binary
     blobs that happen to have an elevated AC₈.
   - Strict "local maximum over smaller strides" rule for shuffle
     niches: AC at the niche stride must beat AC at every smaller
     stride by 10 % to reject broadly-correlated binary content.
   - Early-exit on overwhelmingly ASCII blocks.
2. **Codecs** (each tagged with a 1-byte flavour for self-describing
   round-trip safety):
   - `zstd` directly via libzstd.
   - byte-shuffle 4 / 8 / 16 / 20 + zstd.
   - linear predictor + XOR + internal byte-shuffle (a novel codec
     introduced by this work) for f64 / f32.
   - store + RLE for trivial cases.
3. **Level → strategy mapping**:

   | `level`  | comp_zstd | codec_internal | cascade |
   |----------|----------:|---------------:|---------|
   | 1        |         1 |              1 | smart   |
   | 3 (def)  |         3 |              1 | smart   |
   | 6        |         6 |              2 | always  |
   | 9        |         9 |              3 | always  |
   | 12       |        12 |              6 | always  |
   | 15       |        15 |              9 | always  |
   | 19       |        19 |             12 | always  |
   | 22       |        22 |             15 | always  |

   "comp_zstd" drives plain zstd niches (text / structured / generic).
   "codec_internal" is the trailing zstd inside specialised codecs —
   they get already-decorrelated bytes so a low level is usually enough.
   "cascade" runs plain zstd at user level as a baseline and keeps the
   smaller output.
4. **Multi-block threading** with a pthreads pool. Activated
   automatically when there are at least 4 blocks (so `pthread_create`
   overhead amortises).
5. **Adaptive block size**:
   - < 512 KB → 256 KB blocks
   - 512 KB – 4 MB → 1 MB blocks
   - 4 MB – 32 MB → 4 MB blocks
   - \> 32 MB → 8 MB blocks

## Niche catalogue

| Niche                | Codec                                | Notes |
|----------------------|--------------------------------------|-------|
| `random`             | store; cascade vs zstd               | High entropy |
| `constant`           | custom RLE                           | Near-uniform blocks |
| `numeric_f64`        | predictor + XOR + shuffle 8 + zstd   | Smooth doubles, finite |
| `numeric_f32`        | predictor + XOR + shuffle 4 + zstd   | Smooth floats, finite |
| `numeric_i64`        | byte-shuffle 8 + zstd; cascade       | Timestamps, counters |
| `numeric_i32`        | byte-shuffle 4 + zstd; cascade       | IDs, counters |
| `numeric_i16`        | byte-shuffle 8 + zstd; cascade       | PCM audio |
| `shuffle_4/8/16/20`  | byte-shuffle + zstd; cascade         | Arrays of structs |
| `text`               | zstd at user level                   | Natural text |
| `structured`         | zstd at user level                   | JSON / HTML |
| `generic`            | zstd at user level                   | Honest fallback |

"Cascade" means the codec is also compared against plain zstd at the
user's level and the smaller output is kept. Self-describing per-block
codec output ensures the decoder always picks the right path even
when a fallback fires.

## Quick start

```python
from omnicomp import compress, decompress

with open("my_file.bin", "rb") as f:
    data = f.read()

# Defaults: level=3, threads = min(8, cpu_count()), adaptive block size.
comp = compress(data)
back = decompress(comp)
assert back == data

# Stronger compression:
comp = compress(data, level=12)        # zstd-15-class ratio, ~5x faster
comp = compress(data, level=22)        # near-maximum ratio

# Fully manual:
comp = compress(data, level=9, n_threads=8, block_size=4 * 1024 * 1024)
```

Decompression auto-detects the level and codecs used for each block,
so a stream produced at `level=22` and a stream produced at `level=1`
both decompress with the same `decompress(comp)` call.

## When to pick OmniComp over plain zstd

| Workload                                        | Pick                          |
|--------------------------------------------------|-------------------------------|
| Generic Linux tarballs, small files, want speed  | `zstd --fast=1` or `zstd -1`  |
| Generic, balanced                                | either; `OmniComp L3` ≈ `zstd -3` |
| Numeric / scientific arrays, sensor data         | **OmniComp** (any level)      |
| Want zstd -15 ratio without zstd -15 cost        | **OmniComp L12**              |
| Want maximum ratio without zstd -22 cost         | **OmniComp L19** or **L22**   |
| Decompression-bound serving                      | **OmniComp** (any level)      |

## Build & install

OmniComp depends on **libzstd** and **libpthread**.

### macOS

```bash
brew install zstd
make
```

### Debian / Ubuntu

```bash
sudo apt-get install -y libzstd-dev
make
```

### From source

```bash
make ZSTD_INCLUDE=/path/to/zstd/lib ZSTD_LIB=/path/to/zstd/lib
```

The shared library lands at `omnicomp/libomnicomp_pipeline.{so,dylib}`.

## Testing

```bash
make
python3 tests/test_roundtrip.py     # 15 round-trip tests
python3 tests/test_validation_matrix.py  # system validation tests
# or
pytest tests/ -v
```

`tests/test_roundtrip.py` covers each niche with a synthetic input,
plus boundary sizes (empty, 1 byte, sub-block, multi-block) and a
stress sweep of random binaries. Every test verifies bit-exact
round-trip (`compress → decompress → original`).

For a full project validation matrix (performance + systems behavior),
run:

```bash
python3 examples/validation_matrix.py --level 9 --threads 8 \
  --silesia-dir /tmp/silesia \
  --real-dataset-dir /path/to/real_dataset \
  --output docs/validation_report.md \
  --csv-output docs/validation_report.csv
```

This matrix explicitly tests and documents:

- multiple corpus
- memory used
- real dataset
- multithreading
- small files
- incompressible data
- streaming (chunked frames)
- latency (p50/p95)

See `docs/TESTING.md` for the release gate checklist.

Latest executed validation artifacts:

- `docs/validation_report.md` (generated from `examples/validation_matrix.py`)
- `docs/validation_report.csv` (machine-readable metrics for OmniComp/zstd/lz4)
- `docs/silesia_vs_zstd_metrics.csv` (Silesia blob: zstd vs OmniComp, selected levels)
- `docs/silesia_per_file.csv` (per-file Silesia + blob, same comparison)
- `tests/test_validation_matrix.py` (pass/fail system checks for the same matrix)

## Reproducing the Silesia numbers

```bash
# Get the corpus (one-off)
mkdir -p /tmp/silesia && cd /tmp/silesia
for f in dickens mozilla mr nci ooffice osdb reymont samba sao webster x-ray xml; do
    curl -fsSL -o $f.bz2 "https://sun.aei.polsl.pl/~sdeor/corpus/$f.bz2"
    bzip2 -d -f $f.bz2
done

# Run the benchmark
cd /path/to/zxc
python3 examples/benchmark_silesia.py --silesia-dir /tmp/silesia
```

You will need `pip install zstandard` for the zstd reference. The script writes
`docs/silesia_vs_zstd_metrics.csv` (blob) and `docs/silesia_per_file.csv`
(per-file plus blob), including ratio / throughput deltas and RSS deltas.

## Container format

```
+------+-----------+------------+
| OMN8 | uint32 BE | uint32 BE  |
| (4)  | total len | num blocks |
+------+-----------+------------+
| per block: u8 niche | u32 BE orig_len | u32 BE comp_len | comp_len bytes payload |
| ...
```

Each block independently carries its niche/codec id, so a single
stream can mix specialised codecs across heterogeneous content. Each
specialised codec output ALSO carries a 1-byte flavour tag so a
decoder can distinguish "specialised path" from "fell back to plain
zstd" without depending on the dispatcher's choice.

## Project layout

```
.
├── LICENSE                 # MIT
├── Makefile                # one-shot library build
├── pyproject.toml          # PEP 621 metadata
├── README.md
├── omnicomp/
│   ├── __init__.py             # public Python API (level knob, etc.)
│   ├── _omnicomp.py            # ctypes wrapper for the C pipeline
│   ├── pipeline.c              # full pipeline: detect + dispatch + threading
│   └── detector.c              # standalone reference detector
├── tests/
│   ├── test_roundtrip.py          # per-niche round-trip checks
│   └── test_validation_matrix.py  # system validation checks
├── examples/
│   ├── benchmark.py              # quick ratio + throughput benchmark
│   ├── benchmark_silesia.py      # reproducible Silesia comparison
│   ├── validation_matrix.py      # full validation matrix + CSV/MD export
│   └── level_sweep.py            # optional: OmniComp vs zstd level 1..22 sweep
└── docs/
    ├── paper.docx               # research paper (English + addenda)
    ├── TESTING.md               # release gate and validation workflow
    ├── validation_report.md     # latest validation matrix report
    ├── validation_report.csv    # machine-readable matrix report
    ├── silesia_vs_zstd_metrics.csv  # Silesia blob vs zstd (selected levels)
    └── silesia_per_file.csv       # per-file Silesia + blob vs zstd
```

## Changelog

### v0.9 — strategy ladder

- **Level knob (1..22)** mapped to a per-block strategy. Each level
  picks `comp_zstd`, `codec_internal_level` and a cascade policy.
  `level=1` is fast (zstd-1 everywhere with a smart cascade), `level=22`
  is maximum (zstd-22 with the codec running zstd-15 internally and
  always cascading).
- **Cache-friendly tile transpose** in the byte-shuffle codecs.
- **L12** introduces a new operating point: ~zstd-15 ratio at ~5 × the
  compression speed.
- **L22** matches near-zstd-22 ratio at 47 % faster compression.

### v0.8.1 — correctness

- Fixed a lossy round-trip in the shuffle / pred-shuffle codecs when
  the block size was not divisible by the codec's stride. Each
  specialised codec now writes a 1-byte flavour tag in its output so
  the decoder always knows whether to apply the inverse shuffle. All
  12 Silesia files now round-trip bit-exactly; before this fix
  `mozilla`, `mr` and `osdb` were silently corrupted.
- Detector hardening: numeric pred-shuffle requires AC ≥ 0.15, strict
  local-maxima over smaller strides, no NaN/Inf bit patterns.
- Re-introduced the per-block plain-zstd cascade for specialised
  codecs (had been removed in v8 TURBO for speed).

## Licensing

OmniComp is released under the **MIT** license — see [`LICENSE`](LICENSE).

OmniComp depends on the following permissively-licensed components:

- **zstd** (BSD-3, Meta)
- **pthreads** (system)

All components allow free composition, including for commercial use.

## Contributing

Issues and pull requests are welcome. Please run the test suite (`make
test`) before submitting changes that touch the C pipeline, and keep
the niche IDs in `pipeline.c`, `detector.c` and `_omnicomp.py` in sync.
