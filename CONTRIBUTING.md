# Contributing to OmniComp

Thank you for helping improve OmniComp. This project aims for reproducible
benchmarks, bit-exact lossless compression, and a small, reviewable surface
area—please keep changes focused.

## Prerequisites

- C compiler (`cc` / `clang` / `gcc`) with POSIX threads
- **Zstandard**: either initialize the bundled sources and let `make` build
  them into the pipeline library (no separate libzstd install), **or** install
  system libzstd (e.g. `libzstd-dev` on Debian/Ubuntu, Homebrew `zstd` on macOS)
  and build with `USE_BUNDLED_ZSTD=0` if `third_party/zstd` is absent.
- Python **3.8+**
- **pytest** (for the test suite)

## Build and test

```bash
git submodule update --init --recursive   # once: fetches third_party/zstd
make              # builds omnicomp/libomnicomp_pipeline.{so,dylib}
make test         # pytest on tests/
# or
python3 -m pytest tests/ -q
```

Run `tests/test_roundtrip.py` and `tests/test_validation_matrix.py` before
submitting changes that touch compression, the detector, or the container
format.

## Layout conventions

- **Niche / codec IDs** must stay consistent across `omnicomp/pipeline.c`,
  `omnicomp/detector.c`, and `omnicomp/_omnicomp.py` (`NICHE_NAMES`).
- Prefer **one logical change per PR**; avoid drive-by refactors in unrelated
  files.

## Benchmarks

Silesia-style numbers should come from `examples/benchmark_silesia.py` with
documented thread settings—see `README.md` (“Publication-grade benchmarks”).

## Packaging metadata

If you fork under a different GitHub org or repo name, update the
`[project.urls]` entries in `pyproject.toml` (and any badges you add to
`README.md`) to match your repository.

## Code of conduct

Be constructive and respectful in issues and pull requests.

## Security

See [`SECURITY.md`](SECURITY.md).
