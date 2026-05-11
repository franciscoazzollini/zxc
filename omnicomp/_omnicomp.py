"""OmniComp v8 TURBO: full pipeline in C (detect + compress + threading).

No Python overhead on the critical path. The only Python code involved in
compression is the very thin wrapper below that hands raw memory pointers
to the C pipeline and writes out the container header at the end.
"""
import ctypes
import os
import struct
import sys
from pathlib import Path


def _load_library() -> ctypes.CDLL:
    """Locate and load the compiled OmniComp pipeline shared library.

    Looks for ``libomnicomp_pipeline.{so,dylib}`` next to this module.
    """
    here = Path(__file__).parent
    candidates = [
        here / "libomnicomp_pipeline.so",
        here / "libomnicomp_pipeline.dylib",
    ]
    for path in candidates:
        if path.exists():
            return ctypes.CDLL(str(path))
    raise RuntimeError(
        "Could not find libomnicomp_pipeline.{so,dylib} next to "
        f"{__file__}. Build it with `make` or:\n"
        "  cc -O3 -march=native -ffast-math -fPIC -shared -pthread \\\n"
        "     omnicomp/pipeline.c -lzstd -lm -o omnicomp/libomnicomp_pipeline.so"
    )


LIB = _load_library()

C = ctypes
PU8 = C.POINTER(C.c_uint8)
PU8_PTR = C.POINTER(C.POINTER(C.c_uint8))
PSIZE = C.POINTER(C.c_size_t)
PINT = C.POINTER(C.c_int)

LIB.pipeline_compress_blocks.restype = C.c_int
LIB.pipeline_compress_blocks.argtypes = [
    PU8_PTR, PSIZE, PU8_PTR, PSIZE, PSIZE, PINT,
    C.c_int, C.c_int, C.c_int,
]

LIB.decompress_with_codec.restype = C.c_size_t
LIB.decompress_with_codec.argtypes = [
    PU8, C.c_size_t, C.c_int, PU8, C.c_size_t, C.c_size_t,
]

LIB.my_zstd_compress_bound.restype = C.c_size_t
LIB.my_zstd_compress_bound.argtypes = [C.c_size_t]


# Mirror of the niche IDs in pipeline.c. Keep in sync.
NICHE_NAMES = {
    0: "generic", 1: "random", 2: "constant",
    3: "numeric_f64", 4: "numeric_f32",
    5: "numeric_i64", 6: "numeric_i32", 7: "numeric_i16",
    8: "shuffle_4", 9: "shuffle_8", 10: "shuffle_16", 11: "shuffle_20",
    12: "text", 13: "structured",
    19: "numeric_f64_pred_shuffle",
    21: "numeric_f32_pred_shuffle",
}

MAGIC_V8 = b"OMN8"


def _adaptive_block_size(n: int) -> int:
    """Pick a block size based on total input size.

    Larger inputs use larger blocks: more context for zstd, less header
    overhead, better thread amortisation.
    """
    if n < 512 * 1024:
        return 256 * 1024
    elif n < 4 * 1024 * 1024:
        return 1024 * 1024
    elif n < 32 * 1024 * 1024:
        return 4 * 1024 * 1024
    elif n < 128 * 1024 * 1024:
        return 8 * 1024 * 1024
    # Very large inputs (Silesia-scale or bigger) benefit from larger blocks:
    # zstd gets more context and header overhead is further amortized.
    return 16 * 1024 * 1024


def omni_compress_v8_turbo(data: bytes, n_threads: int = 0,
                           block_size: int = 0, zstd_level: int = 3) -> bytes:
    """Compress ``data`` with OmniComp v8 TURBO.

    Args:
        data:        Raw bytes to compress.
        n_threads:   Worker threads in the C pool. ``0`` picks
                     ``min(8, cpu_count())``.
        block_size:  Bytes per block. ``0`` picks an adaptive size based
                     on ``len(data)``.
        zstd_level:  zstd compression level used internally (default 3).

    Returns:
        A self-describing v8 container (magic + per-block metadata + payloads).
    """
    n = len(data)
    if n == 0:
        return MAGIC_V8 + struct.pack(">II", 0, 0)
    if n_threads == 0:
        n_threads = min(8, os.cpu_count() or 1)
    if block_size == 0:
        block_size = _adaptive_block_size(n)

    # Split into blocks (zero-copy via memoryview).
    n_blocks = (n + block_size - 1) // block_size
    data_ba = bytearray(data)  # mutable so ctypes can take from_buffer

    # Build pointers into the same bytearray (zero-copy).
    src_ptrs = (C.POINTER(C.c_uint8) * n_blocks)()
    src_lens = (C.c_size_t * n_blocks)()

    base_arr = (C.c_uint8 * len(data_ba)).from_buffer(data_ba)
    base_addr = C.addressof(base_arr)
    for i in range(n_blocks):
        offset = i * block_size
        block_len = min(block_size, n - offset)
        src_ptrs[i] = C.cast(base_addr + offset, C.POINTER(C.c_uint8))
        src_lens[i] = block_len

    # Pre-allocated destination buffers.
    caps = [LIB.my_zstd_compress_bound(min(block_size, n - i * block_size)) + 256
            for i in range(n_blocks)]
    total_cap = sum(caps)
    dst_ba = bytearray(total_cap)
    dst_arr = (C.c_uint8 * total_cap).from_buffer(dst_ba)
    dst_base = C.addressof(dst_arr)

    dst_ptrs = (C.POINTER(C.c_uint8) * n_blocks)()
    dst_caps = (C.c_size_t * n_blocks)()
    offset = 0
    for i in range(n_blocks):
        dst_ptrs[i] = C.cast(dst_base + offset, C.POINTER(C.c_uint8))
        dst_caps[i] = caps[i]
        offset += caps[i]

    dst_writtens = (C.c_size_t * n_blocks)()
    chosen = (C.c_int * n_blocks)()

    # Threading only pays off once we have at least 4 blocks; fewer
    # would spend more in pthread_create than they recover.
    actual_threads = n_threads if n_blocks >= 4 else 1
    rc = LIB.pipeline_compress_blocks(
        src_ptrs, src_lens, dst_ptrs, dst_caps, dst_writtens, chosen,
        n_blocks, zstd_level, actual_threads,
    )
    if rc != 0:
        raise RuntimeError("pipeline_compress_blocks failed")

    # Assemble the container. This is Python, but only runs once at the end.
    out = bytearray()
    out += MAGIC_V8
    out += struct.pack(">I", n)
    out += struct.pack(">I", n_blocks)

    offset = 0
    for i in range(n_blocks):
        nid = chosen[i]
        block_len = src_lens[i]
        comp_len = dst_writtens[i]
        out += struct.pack(">B", nid)
        out += struct.pack(">II", block_len, comp_len)
        out += bytes(dst_ba[offset:offset + comp_len])
        offset += caps[i]
    return bytes(out)


def omni_decompress_v8(comp: bytes) -> bytes:
    """Decompress an OmniComp v8 container produced by ``omni_compress_v8_turbo``."""
    assert comp[:4] == MAGIC_V8, f"Bad magic: {comp[:4]!r}"
    total = struct.unpack(">I", comp[4:8])[0]
    n_blocks = struct.unpack(">I", comp[8:12])[0]

    if total == 0:
        return b""

    out = bytearray(total)
    out_arr = (C.c_uint8 * total).from_buffer(out)
    out_base = C.addressof(out_arr)

    pos = 12
    out_offset = 0
    for _ in range(n_blocks):
        nid = comp[pos]; pos += 1
        orig, comp_size = struct.unpack(">II", comp[pos:pos + 8]); pos += 8
        payload = comp[pos:pos + comp_size]; pos += comp_size

        # Decode through C.
        src_ba = bytearray(payload)
        src_arr = (C.c_uint8 * len(src_ba)).from_buffer(src_ba)
        dst_ptr = C.cast(out_base + out_offset, C.POINTER(C.c_uint8))

        written = LIB.decompress_with_codec(src_arr, len(payload), nid,
                                            dst_ptr, total - out_offset, orig)
        if written != orig:
            raise RuntimeError(f"decompress mismatch: {written} != {orig}")
        out_offset += orig
    return bytes(out)


# Self-test entry point (used historically; the canonical test lives in
# ``tests/test_roundtrip.py``).
if __name__ == "__main__":
    import time

    base = Path(__file__).parent.parent

    print("=== OmniComp v8 TURBO (100% C pipeline) ===")
    print(f"{'File':<28} {'Ratio':>8} {'Comp MB/s':>10} {'Decomp MB/s':>13}")
    print("-" * 65)

    any_corpus = False
    for d in ["corpus", "corpus_numeric", "corpus_extra", "corpus_large"]:
        cpath = base / d
        if not cpath.exists():
            continue
        any_corpus = True
        for f in sorted(cpath.iterdir()):
            if not f.is_file():
                continue
            data = f.read_bytes()
            if len(data) < 100:
                continue
            mb = len(data) / (1024 * 1024)

            t0 = time.perf_counter()
            for _ in range(3):
                c = omni_compress_v8_turbo(data)
            t_c = (time.perf_counter() - t0) / 3

            t0 = time.perf_counter()
            for _ in range(3):
                back = omni_decompress_v8(c)
            t_d = (time.perf_counter() - t0) / 3

            ok = back == data
            r = len(c) / len(data)
            print(f"{f.name:<28} {r:>8.4f} {mb / t_c:>10.1f} {mb / t_d:>13.1f}  "
                  f"{'OK' if ok else 'FAIL'}")

    if not any_corpus:
        print("(no corpus directories found; run tests/test_roundtrip.py "
              "for synthetic round-trip checks)", file=sys.stderr)
