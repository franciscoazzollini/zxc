# Beyond zstd: A Lossless Compressor That Pushes the Pareto Frontier

*MIT-licensed, block-adaptive compression: often faster decode than zstd at the same level, within a few percent on ratio—reproducible on Silesia.*

---
**Poner una imagen de (banner / portada Medium):** caja de software retro tipo WinRAR/WinZip años 90–2000, pero modernizada: libros y carpetas apretándose en un bloque compacto, logo “OmniComp” o “zxc”, sensación de compresión sin pérdidas y velocidad.

**Prompt sugerido (IA / diseñador):** Medium article hero banner 16:9, nostalgic late-90s shareware box art meets clean 2024 editorial design, stylized stack of colorful file folders and hardcover books being squeezed into a glowing compressed archive cube with a subtle zipper or clamp motif, bold product name “OmniComp” in chunky retro software typography, tagline area left subtle, teal and amber accents on dark navy background, slight gloss and depth like classic WinRAR WinZip splash screens but not copying any real brand logos, flat-meets-3D illustration, high contrast readable at thumbnail size, no tiny illegible text, professional tech blog cover not photorealistic clutter.
---

Cold storage is cheap. **Reading it back is not.**

Every analytics pipeline, backup restore, and edge cache miss pays the same tax: decompress bytes before you can use them. For a decade, teams reached for **Zstandard (zstd)**—fast, predictable, battle-tested. You pick a level from 1 to 22, trade ratio for CPU, ship. That mental model works so well it became infrastructure folklore.

But real files are not uniform. A tarball might mix JSON logs, protobuf headers, float arrays, and high-entropy noise in a single stream. One excellent generic codec applies one family of ideas everywhere. The uncomfortable question is whether we leave speed and ratio on the table because we never **reshape** the data before the final compression step.

We built **OmniComp** to test that question honestly—and to publish the answer with reproducible artifacts, not slide-deck adjectives.

The workloads we care about are decompression-first: scientific arrays fetched on every query, mixed archives expanded at restore time, telemetry bundles read far more often than they are written. In those paths, a few percent on ratio matters less than **how many megabytes per second** you get back on a single core—and whether compression at ingest time finished before the maintenance window closed.

## Why zstd still wins—and where it plateaus

Zstd earned its place. The level ladder is a shared map: higher level, better ratio, more compression time. Decompression stays fast across the range. That consistency is why it powers databases, package managers, and log pipelines worldwide.

The limitation is conceptual, not a flaw in engineering. Heterogeneous bytes respond differently to preprocessing. Text loves literal matching. Stride-structured integers compress better after **byte-shuffle** (reordering bytes so correlations cluster). Smooth floating-point series benefit from lightweight **prediction** before entropy coding. Random-looking segments should fall back to plain storage or generic zstd—not forced through a niche that guesses wrong.

OmniComp does not try to out-invent zstd’s entropy coding. It tries to **route** each block to a better presentation, then let zstd (or a store path) finish the job—**only when the specialized route actually wins**.

Engineers talk about the **Pareto frontier**: the set of choices where you cannot improve one metric without hurting another. Plain zstd already traces a remarkable frontier across its level knob. OmniComp’s claim is narrower and testable: on heterogeneous corpora like Silesia, you can often **shift that frontier**—similar ratio at the same level number, faster decode, or zstd-15/22-class ratio without paying the full zstd-15/22 compress bill.

## The idea in one sentence

**Split the input into blocks, classify each block, compress with a niche-aware codec, and keep plain zstd whenever the niche underperforms.**

That last clause matters. Without it, adaptive compression becomes a liability: clever heuristics that occasionally inflate files. OmniComp’s **competitive cascade** recompresses the original block bytes with plain zstd at a reference level and keeps whichever output is smaller. Wrong classification does not punish you below the zstd floor you already trust.

The hot path—detection, codec dispatch, threading, assembly—runs in **100% C** inside `libomnicomp_pipeline`. Python only wraps the API and writes the self-describing **OMN8** container header. Decompression reads those per-block tags and inverts exactly what compression chose. No guessing at decode time.

Blocks are sized adaptively (larger inputs use larger blocks so detector overhead stays negligible and zstd sees useful context). Compression can use multiple worker threads across blocks; the container records enough metadata that decode remains deterministic whether or not you enable internal block parallelism—which is exactly what we pin off when publishing single-thread decode comparisons.

## How it works (without opening the codebase)

Think of three layers:

**1. Niche detector (~50 µs per block on large inputs).**  
A single pass builds a histogram, samples autocorrelation at strides 2/4/8/16/20, checks for ASCII-heavy text, and applies conservative rules (for example, shuffle niches must beat smaller strides by a margin). The goal is cheap, stable labels—not perfect ML.

**2. Codec dispatch.**  
Each niche maps to a concrete path: plain zstd for text and generic content; byte-shuffle + internal zstd for stride-structured data; predictor + XOR + shuffle + zstd for correlated floats; store or minimal paths for noise. Every output carries a one-byte flavour tag so decode is deterministic.

**3. Cascade.**  
For many specialized paths, the pipeline also runs plain zstd on the **same original block** and compares sizes. Smaller wins. Often that means zstd generic—exactly the safe outcome you want when the detector is uncertain.

The user-facing `level` **knob (1…22)** still feels like zstd’s ladder. Under the hood it adjusts which zstd levels run in generic niches, which levels run inside specialized codecs, and how aggressively the cascade compares candidates—one dial, multiple internal strategies.

---

**Poner una imagen de:** un diagrama de flujo limpio: archivo → bloques → detector de nicho → códec especializado → cascada vs zstd → contenedor OMN8.

## **Prompt sugerido (IA / diseñador):** Minimal technical editorial illustration, dark slate background, soft teal and amber accents, horizontal flowchart: raw file splits into blocks → niche detector (text / numeric / shuffle) → specialized codec path → competitive cascade comparing size with plain zstd → OMN8 container output, flat vector style, no photorealism, no tiny unreadable labels, 16:9, suitable for Medium hero section.

> If the specialized path does not beat plain zstd on the same bytes, we keep zstd. The system is conservative by design.



## Why you should trust these benchmarks

Adaptive compressors invite skepticism—and should. A multithreaded block decoder can look faster than single-threaded libzstd even when the algorithm is not better, simply because more cores touched the work.

Our committed Silesia CSVs (`docs/silesia_vs_zstd_metrics.csv`, `docs/silesia_per_file.csv`) record `n_threads`, and when `omni_seq_block_decode = 1`, OmniComp’s OMN8 decoder ran **without** internal pthread parallelism between blocks—matching single-threaded libzstd decompress for fair headline comparisons.

They also publish `decomp_mb_s_ratio_omni_over_zstd` (Omni throughput ÷ zstd throughput). When that ratio is **3.0**, the percentage column reads about **+200%**—that is how relative speed is defined, not hidden multicore inflation.

**Measurement context:** Apple Silicon (M-series), zstd **1.5.6**, OmniComp built with `cc -O3 -march=native`, **best of 3** runs via `examples/benchmark_silesia.py`. Ratio uses the zstd convention (**original ÷ compressed**; higher is better). Corpus: the standard [Silesia](https://sun.aei.polsl.pl/~sdeor/index.php?page=silesia) set (~202 MB aggregated blob).

Skeptics should clone, `make`, and replay the same script—not trust a blog post.

Benchmarks compress each Silesia file as an independent stream, time compress and decompress in isolated runs, and aggregate a **blob row** for headline comparisons. Per-file rows in `docs/silesia_per_file.csv` expose where wins come from (structured files like `x-ray`, text-heavy files, and generic segments that stay on plain zstd). When you evaluate OmniComp for production, start there: one aggregate number is never the whole story.

## Headline results on Silesia (single thread, fair decode)

On the full heterogeneous blob, OmniComp usually stays within a few percent of zstd’s ratio at the **same numeric level**, while often delivering higher decompress throughput. Three operating points tell the story:


| Level | zstd ratio | OmniComp ratio | zstd decomp MB/s | OmniComp decomp MB/s | Takeaway                                                     |
| ----- | ---------- | -------------- | ---------------- | -------------------- | ------------------------------------------------------------ |
| 9     | 3.588      | 3.541          | 1 843            | **2 166**            | ~zstd -9 ratio, **~+18% decode**                             |
| 12    | 3.727*     | 3.603          | 2 022*           | **2 248**            | zstd-**15-class** ratio at **~4.8×** zstd -15 compress speed |
| 22    | 4.054      | 3.927          | 1 767            | **1 919**            | Near zstd -22 ratio, **~47% less** compress wall time        |


At level 12, compare OmniComp L12 to zstd **-15** (the ratio tier it tracks); zstd -15 ratio 3.727 vs OmniComp 3.603 (~3% behind), zstd -15 compress ~13 MB/s vs OmniComp ~64 MB/s.

Across levels 1…22 on this corpus, OmniComp is typically within **~1–3%** of zstd ratio while decompressing faster on many rows; peak decode wins in committed per-file data reach about **+39%** vs zstd at the same level.

---

**Poner una imagen de:** gráfico editorial ratio vs velocidad de descompresión: OmniComp L9/L12/L22 frente a zstd al mismo nivel, mostrando mejor decode sin sacrificar ratio.

## **Prompt sugerido (IA / diseñador):** Clean data visualization for a tech blog, scatter or grouped bar chart, x-axis compression ratio, y-axis decompress MB/s, two series zstd vs OmniComp at levels 9 12 22, muted professional palette white background, large readable axis titles, editorial infographic not 3D, 16:9, no logo watermark. Annotate three callouts: L9 similar ratio faster decode, L12 zstd-15-class ratio, L22 near-max ratio faster compress wall time.



## Case study: when structure is obvious (`x-ray`)

Benchmark aggregates hide wins. The Silesia `x-ray` file (16-bit medical imaging, ~8 MB) is stride-structured numeric data—the shape OmniComp was designed for.


| Algorithm       | Ratio    | Decomp MB/s |
| --------------- | -------- | ----------- |
| zstd -3         | 1.39     | 1 053       |
| zstd -9         | 1.58     | 990         |
| **OmniComp L3** | **1.74** | **1 902**   |


OmniComp selects **byte-shuffle 8**, splitting the stream into eight sub-bands that zstd compresses far more effectively. Ratio improves **~25% vs zstd -3**; decode improves **~80%** because de-shuffle is essentially a memcpy-class inverse—not another heavy entropy pass.

That is the design pattern in miniature: cheap transform, familiar zstd finish, fast decode.

A second pattern shows up at high levels. If your pipeline needs **zstd -15-class** compression but cannot afford ~13 MB/s compress on the full blob, **OmniComp L12** lands near **3.603 ratio** (vs **3.727** for zstd -15, about **3%** behind) at **~64 MB/s** compress—roughly **5×** faster than zstd -15—while decompressing at **~2 248 MB/s** (**+11%** vs zstd -15). At the top, **L22** reaches **~3.927 ratio** at **~5.3 MB/s** compress versus zstd -22’s **~3.6 MB/s**—essentially the same ratio tier with **~47%** less compress wall time.

## What we are not claiming

Intellectual honesty is part of the benchmark story.

**OmniComp is not a universal zstd replacement.** At very fast generic settings (levels **1–2**), zstd often leads on pure compression throughput for generic blobs. When most blocks already choose plain zstd inside the cascade, margins shrink—expected on data that is already generic.

**This is research-grade open source with a reproducibility trail**, not a certified production stamp for every deployment. Integrators should profile their own mixes, version the container format, and treat numbers on Silesia as evidence—not prophecy.

**We still depend on zstd’s code and license upstream.** OmniComp is MIT-licensed; the pipeline uses Meta’s Zstandard (vendored from `third_party/zstd` by default and linked statically into the shared library so deployments do not require a separate system libzstd). See `NOTICE` in the repository for upstream terms.

If your KPI is **fastest generic compression**, plain zstd remains the pragmatic default. If your KPI is **decompression speed with strong ratio** on mixed or numeric data—or you want zstd-15/22-class ratios without zstd-15/22-class compress times—OmniComp is worth a measured look.

Practical guidance from our own benchmarks: levels **9–19** tend to balance ratio and decode on Silesia; **L12** is the “strong but fast tier” sweet spot; numeric or scientific data you read repeatedly is the strongest fit.

## Try it yourself (ten minutes)

Everything lives in the open repository: [github.com/omnicomp/omnicomp](https://github.com/omnicomp/omnicomp)

```bash
git clone https://github.com/omnicomp/omnicomp.git
cd omnicomp
git submodule update --init --recursive   # bundled zstd
make
pip install -e .
python examples/benchmark_silesia.py      # replay Silesia numbers
zxc /path/to/your/file                    # CLI compress → file.zxc
```

Round-trip correctness is guarded by pytest suites on synthetic niches and full Silesia files. Committed CSVs in `docs/` should match your replay when hardware differs only in absolute MB/s—not in the shape of the trade-offs.

Found a regression or a dataset where cascade chooses wrong? Open an issue with the file fingerprint and level. Small, reproducible reports beat hot takes.

## A possible next tier—not a new religion

Infrastructure moves when a tool respects what came before while extending what is possible. OmniComp keeps zstd’s level metaphor and zstd’s frames wherever generic paths win. It adds block adaptation and a conservative cascade so specialized transforms only survive when they earn their bytes.

For workloads that **read cold storage repeatedly**—scientific arrays, mixed archives, logs with structured pockets—that is a practical next step after “just use zstd.” Not because compression magic returned, but because **presentation before entropy coding** still has room on real heterogeneous data.

The final verdict is not this article. Clone the repo, rerun the benchmarks, and point OmniComp at your own files. Breakthroughs that matter are the ones you can **reproduce**.

---

*OmniComp v0.9 · MIT License · Benchmark artifacts:* `docs/silesia_vs_zstd_metrics.csv`*,* `docs/silesia_per_file.csv`